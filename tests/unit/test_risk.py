"""风控单元测试(V2.0: 百分比风控 + 异常保护)"""

import time

import pytest

from at60_risk.risk_breaker import CircuitBreaker
from at60_risk.risk_drawdown import DrawdownController
from at60_risk.risk_manager import RiskManager
from at60_risk.risk_position import PositionManager
from at50_strategy.strategy_base import Signal, SignalSide


def make_signal(side=SignalSide.BUY, symbol="BTCUSDT", price=100.0, qty=None, quote=None, reason=None):
    return Signal(
        symbol=symbol,
        strategy="test",
        side=side,
        price=price,
        quantity=qty,
        quote_amount=quote,
        reason=reason or [],
    )


class TestPositionManager:
    def test_buy_updates_avg(self):
        pm = PositionManager()
        pm.apply_buy("BTCUSDT", 1.0, 100.0)
        pm.apply_buy("BTCUSDT", 1.0, 200.0)
        pos = pm.get("BTCUSDT")
        assert pos.quantity == 2.0
        assert pos.avg_price == 150.0
        assert pos.peak_price == 200.0

    def test_sell_realizes_pnl(self):
        pm = PositionManager()
        pm.apply_buy("BTCUSDT", 2.0, 100.0)
        pos, pnl = pm.apply_sell("BTCUSDT", 1.0, 150.0)
        assert pnl == pytest.approx(50.0)
        assert pos.quantity == 1.0
        assert pos.realized_pnl == pytest.approx(50.0)

    def test_sell_cannot_oversell(self):
        pm = PositionManager()
        pm.apply_buy("BTCUSDT", 1.0, 100.0)
        pos, _ = pm.apply_sell("BTCUSDT", 5.0, 120.0)
        assert pos.quantity == 0.0

    def test_empty_position_none(self):
        pm = PositionManager()
        assert pm.get_or_none("BTCUSDT") is None

    def test_peak_price_update(self):
        pm = PositionManager()
        pm.apply_buy("BTCUSDT", 1.0, 100.0)
        pm.update_price("BTCUSDT", 180.0)
        assert pm.get("BTCUSDT").peak_price == 180.0

    def test_sellable_quantity(self):
        pm = PositionManager()
        assert pm.sellable_quantity("BTCUSDT") == 0.0
        pm.apply_buy("BTCUSDT", 2.0, 100.0)
        assert pm.sellable_quantity("BTCUSDT") == 2.0

    def test_buyable_quote(self):
        """V2.0: 可买额度 = min(现金, 仓位限额剩余)"""
        pm = PositionManager()
        # 空仓: min(1000现金, 2000限额) = 1000
        assert pm.buyable_quote("BTCUSDT", 1000.0, 2000.0, 100.0) == 1000.0
        # 持仓 1.0@100(=1000), 限额 2000 -> 剩 1900, 现金充足给 1900
        pm.apply_buy("BTCUSDT", 1.0, 100.0)
        assert pm.buyable_quote("BTCUSDT", 5000.0, 2000.0, 100.0) == 1900.0
        # 现金不足时取现金
        assert pm.buyable_quote("BTCUSDT", 300.0, 2000.0, 100.0) == 300.0
        # 限额 1000 - 持仓 100 -> 剩 900
        assert pm.buyable_quote("BTCUSDT", 5000.0, 1000.0, 100.0) == 900.0

    def test_position_report(self):
        """V2.0: 持仓报告"""
        pm = PositionManager()
        pm.apply_buy("BTCUSDT", 2.0, 100.0)
        report = pm.position_report("BTCUSDT", 150.0)
        assert report["quantity"] == 2.0
        assert report["avg_cost"] == 100.0
        assert report["market_price"] == 150.0
        assert report["unrealized_profit"] == 100.0
        assert report["profit_ratio"] == 0.5
        assert report["market_value"] == 300.0


class TestDrawdown:
    def test_peak_tracking(self):
        dc = DrawdownController(initial_equity=100000.0)
        dd, breach = dc.update(110000.0)
        assert dd == 0.0 and not breach
        dd, breach = dc.update(100000.0)
        assert dd == pytest.approx(1 / 11)  # (110k-100k)/110k

    def test_breach(self):
        """V2.0: 默认 15% 回撤熔断"""
        dc = DrawdownController(initial_equity=100000.0)
        dc.update(100000.0)
        dd, breach = dc.update(84000.0)  # 16% 回撤
        assert dd >= 0.15
        assert breach

    def test_no_breach_at_10pct(self):
        """V2.0: 10% 回撤不再触发(阈值提高到 15%)"""
        dc = DrawdownController(initial_equity=100000.0)
        dc.update(100000.0)
        dd, breach = dc.update(89000.0)  # 11%
        assert not breach


class TestCircuitBreaker:
    def test_trip_on_drawdown(self):
        cb = CircuitBreaker()
        assert not cb.is_open
        cb.check_drawdown(0.16)
        assert cb.is_open
        assert "回撤" in cb.reason

    def test_cooldown_expires(self, monkeypatch):
        cb = CircuitBreaker()
        cb.check_drawdown(0.16)
        assert cb.is_open
        future = time.time() + cb.cooldown_seconds + 1
        monkeypatch.setattr(time, "time", lambda: future)
        assert not cb.is_open

    def test_daily_loss_trip(self):
        cb = CircuitBreaker()
        cb._day_start_equity = 100000.0
        assert cb.check_daily_loss(94000.0)  # -6% > 5%

    def test_manual_trip_and_reset(self):
        cb = CircuitBreaker()
        cb.manual_trip("测试")
        assert cb.is_open
        cb.reset()
        assert not cb.is_open


class TestRiskManager:
    async def test_buy_approved(self):
        rm = RiskManager()
        # 默认百分比: 权益 100k * 5% = 5000 单笔限额
        sig = make_signal(qty=1.0, price=100.0)
        d = await rm.check(sig, {"BTCUSDT": 100.0})
        assert d.approved
        assert d.quantity == 1.0

    async def test_buy_position_limit_pct(self):
        """V2.0: 40% 权益持仓限额 = 40000"""
        rm = RiskManager()
        rm.positions.apply_buy("BTCUSDT", 399.0, 100.0)  # 39900
        sig = make_signal(qty=10.0, price=100.0)  # 想再买 1000
        d = await rm.check(sig, {"BTCUSDT": 100.0})
        assert d.approved
        assert d.quantity == pytest.approx(1.0)  # 缩量到剩余 100

    async def test_buy_rejected_when_full(self):
        rm = RiskManager()
        rm.positions.apply_buy("BTCUSDT", 400.0, 100.0)  # 满 40000
        sig = make_signal(qty=1.0, price=100.0)
        d = await rm.check(sig, {"BTCUSDT": 100.0})
        assert not d.approved
        assert "持仓" in d.reason

    async def test_sell_without_position_rejected(self):
        rm = RiskManager()
        sig = make_signal(side=SignalSide.SELL, qty=1.0, price=100.0)
        d = await rm.check(sig, {"BTCUSDT": 100.0})
        assert not d.approved

    async def test_sell_capped_to_position(self):
        rm = RiskManager()
        rm.positions.apply_buy("BTCUSDT", 1.0, 100.0)
        sig = make_signal(side=SignalSide.SELL, qty=5.0, price=110.0)
        d = await rm.check(sig, {"BTCUSDT": 110.0})
        assert d.approved
        assert d.quantity == 1.0

    async def test_breaker_blocks_all(self):
        rm = RiskManager()
        rm.breaker.manual_trip("测试熔断")
        sig = make_signal(qty=1.0, price=100.0)
        d = await rm.check(sig, {"BTCUSDT": 100.0})
        assert not d.approved
        assert "熔断" in d.reason

    async def test_min_notional_reject(self):
        rm = RiskManager()
        sig = make_signal(qty=0.01, price=100.0)  # 1 USDT
        d = await rm.check(sig, {"BTCUSDT": 100.0})
        assert not d.approved

    async def test_observe_signal_not_executed(self):
        """V2.0: 观察档信号(60-80分)不执行"""
        rm = RiskManager()
        sig = make_signal(qty=1.0, price=100.0, reason=["观察档(72分,未达80)", "CVD上升"])
        d = await rm.check(sig, {"BTCUSDT": 100.0})
        assert not d.approved
        assert d.observed
        assert rm.observe_count == 1

    def test_equity_calculation(self):
        rm = RiskManager()
        rm.positions.apply_buy("BTCUSDT", 1.0, 100.0)
        assert rm.equity({"BTCUSDT": 150.0}) == pytest.approx(100050.0)

    def test_percentage_limits(self):
        """V2.0: 百分比限额计算"""
        rm = RiskManager()
        rm.breaker.current_equity = 100000.0
        assert rm.max_position_quote == pytest.approx(40000.0)  # 40%
        assert rm.max_single_order_quote == pytest.approx(5000.0)  # 5%

    def test_v1_absolute_override(self):
        """V1 兼容: 绝对金额优先于百分比"""
        rm = RiskManager()
        rm.settings.risk_max_position_quote = 20000.0
        rm.settings.risk_max_single_order_quote = 2000.0
        assert rm.max_position_quote == 20000.0
        assert rm.max_single_order_quote == 2000.0


class TestAnomalyProtection:
    """V2.0: 异常保护"""

    def test_price_spike_pauses(self):
        rm = RiskManager()
        # 正常价格序列
        assert not rm.check_tick_anomaly("BTCUSDT", 100.0)
        assert not rm.check_tick_anomaly("BTCUSDT", 100.1)
        # 瞬间 3% 波动
        assert rm.check_tick_anomaly("BTCUSDT", 103.2)
        assert rm.anomaly_paused
        assert "价格瞬间波动" in rm._anomaly_reason

    async def test_paused_rejects_signals(self):
        rm = RiskManager()
        rm._pause("测试异常")
        assert rm.anomaly_paused
        sig = make_signal(qty=1.0, price=100.0)
        d = await rm.check(sig, {"BTCUSDT": 100.0})
        assert not d.approved
        assert "异常保护" in d.reason

    def test_pause_expires(self, monkeypatch):
        rm = RiskManager()
        rm._pause("测试")
        future = time.time() + rm.settings.risk_anomaly_pause_seconds + 1
        monkeypatch.setattr(time, "time", lambda: future)
        assert not rm.anomaly_paused

    def test_market_silence(self):
        rm = RiskManager()
        # 无 tick: 不触发
        assert not rm.check_market_silence()
        # 模拟 60 秒前的 tick
        rm._last_tick_time = time.time() - 60
        assert rm.check_market_silence()
        assert rm.anomaly_paused

    def test_consecutive_execution_errors(self):
        rm = RiskManager()
        for _ in range(3):
            rm.record_execution_error()
        assert rm.anomaly_paused
        # 成功后清零
        rm.record_execution_success()
        assert rm._consecutive_errors == 0

    def test_status_includes_anomaly(self):
        rm = RiskManager()
        status = rm.status()
        assert "anomaly_paused" in status
        assert "equity" in status
        assert "observe_count" in status
