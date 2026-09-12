"""V9.0 组合编排层单元测试: PortfolioManager(三桶) + CorePositionManager(核心仓决策)"""

from types import SimpleNamespace

import pytest

from at40_portfolio.core_manager import CoreAction, CorePositionManager
from at40_portfolio.portfolio_manager import PortfolioManager
from at50_risk.risk_buckets import BucketPositionManager, CORE, TRADE
from at50_risk.risk_position import PositionManager


def _make_managers():
    pm = PositionManager()
    bm = BucketPositionManager(pm)
    return pm, bm


class TestPortfolioManager:
    def test_target_three_buckets(self):
        """三桶目标: 核心 40% / 交易 30% / 现金 30%"""
        pm, bm = _make_managers()
        mgr = PortfolioManager(pm, bm)
        equity, price = 10000.0, 100.0
        assert mgr.target_core_qty(equity, price) == pytest.approx(40.0)   # 4000/100
        assert mgr.target_trade_qty(equity, price) == pytest.approx(30.0)  # 3000/100
        assert mgr.target_cash(equity) == pytest.approx(3000.0)

    def test_evaluate_diffs(self):
        """评估偏离: 目标 - 当前"""
        pm, bm = _make_managers()
        bm.on_buy_fill("SOLUSDT", 10.0, 100.0, CORE)
        bm.on_buy_fill("SOLUSDT", 5.0, 100.0, TRADE)
        mgr = PortfolioManager(pm, bm)
        ev = mgr.evaluate("SOLUSDT", 10000.0, 100.0)
        assert ev["target_core_qty"] == pytest.approx(40.0)
        assert ev["core_diff"] == pytest.approx(30.0)   # 40 - 10
        assert ev["trade_diff"] == pytest.approx(25.0)  # 30 - 5

    def test_zero_price_guards(self):
        pm, bm = _make_managers()
        mgr = PortfolioManager(pm, bm)
        assert mgr.target_core_qty(10000.0, 0.0) == 0.0
        assert mgr.target_trade_qty(10000.0, 0.0) == 0.0


def _analytics(trend="up", ema_fast=120.0, ema_slow=100.0, regime="BULL"):
    return SimpleNamespace(trend=trend, ema_fast=ema_fast, ema_slow=ema_slow, regime=regime)


def _assessment(regime="BULL", btc_trend="up"):
    return SimpleNamespace(regime=regime, btc_trend=btc_trend)


class TestCorePositionManager:
    def test_add_when_conditions_met(self):
        """趋势向上 + BTC 走强 + 非 PANIC -> ADD"""
        _, bm = _make_managers()
        cm = CorePositionManager(bm)
        d = cm.decide("SOLUSDT", _analytics(), _assessment(), 100.0, 100.0)
        assert d["action"] == CoreAction.ADD
        assert d["add_qty"] == pytest.approx(100.0)

    def test_reduce_on_panic(self):
        """PANIC -> 核心仓趋势破坏保护 REDUCE"""
        _, bm = _make_managers()
        bm.on_buy_fill("SOLUSDT", 50.0, 100.0, CORE)
        cm = CorePositionManager(bm)
        d = cm.decide("SOLUSDT", _analytics(), _assessment(regime="PANIC"), 100.0, 100.0)
        assert d["action"] == CoreAction.REDUCE
        assert d["reduce_qty"] == pytest.approx(50.0)

    def test_reduce_on_ema_death_cross(self):
        """EMA 死叉(有持仓) -> REDUCE"""
        _, bm = _make_managers()
        bm.on_buy_fill("SOLUSDT", 50.0, 100.0, CORE)
        cm = CorePositionManager(bm)
        d = cm.decide("SOLUSDT", _analytics(ema_fast=90.0, ema_slow=100.0), _assessment(), 100.0, 100.0)
        assert d["action"] == CoreAction.REDUCE

    def test_hold_btc_down(self):
        """BTC 锚走弱 -> 不建仓"""
        _, bm = _make_managers()
        cm = CorePositionManager(bm)
        d = cm.decide("SOLUSDT", _analytics(), _assessment(btc_trend="down"), 100.0, 100.0, btc_change_24h=-3.0)
        assert d["action"] == CoreAction.HOLD

    def test_hold_no_uptrend(self):
        """未站上长期趋势 -> HOLD"""
        _, bm = _make_managers()
        cm = CorePositionManager(bm)
        d = cm.decide("SOLUSDT", _analytics(ema_fast=90.0, ema_slow=100.0), _assessment(), 100.0, 100.0)
        assert d["action"] == CoreAction.HOLD

    def test_hold_at_target(self):
        """已达目标 -> HOLD"""
        _, bm = _make_managers()
        bm.on_buy_fill("SOLUSDT", 100.0, 100.0, CORE)
        cm = CorePositionManager(bm)
        d = cm.decide("SOLUSDT", _analytics(), _assessment(), 100.0, 100.0)
        assert d["action"] == CoreAction.HOLD
