"""V9.0 M2.1: Regime 6 态(BULL/NORMAL/SIDEWAY/VOLATILE/BEAR/PANIC)一致性测试

验证: 分类器产出 6 态、各系数表对新态均有定义且单调, 不回归旧 4 态行为。
"""

import pytest

from at30_analytics.regime import MarketRegimeEngine
from at50_strategy.strategy_decision import DecisionEngine
from at60_risk.risk_allocation import EXPOSURE_TABLE, PortfolioAllocator
from at60_risk.risk_sizing import REGIME_SIZE_FACTOR, PositionSizer

ALL_SIX = ("BULL", "NORMAL", "SIDEWAY", "VOLATILE", "BEAR", "PANIC")


def _evaluate(vol_hi: float, vol_lo: float, **kw) -> str:
    """构造中性市场(无趋势)并按给定高低价返回分类 regime"""
    eng = MarketRegimeEngine()
    r = eng.evaluate(
        symbol="SOLUSDT",
        symbol_trend="neutral",
        symbol_ema_fast=100.0,
        symbol_ema_slow=100.0,
        recent_high=vol_hi,
        recent_low=vol_lo,
        volume_ratio=1.0,
        delta_ratio=0.0,
        cvd_rising=False,
        **kw,
    )
    return r.regime


class TestRegimeClassifier6State:
    def test_neutral_zone_three_tiers(self):
        # NORMAL: 振幅 <1.0%
        assert _evaluate(100.4, 99.6) == "NORMAL"
        # SIDEWAY: 1.0% ~ 1.5%
        assert _evaluate(100.7, 99.5) == "SIDEWAY"
        # VOLATILE: ≥1.5%
        assert _evaluate(101.0, 99.4) == "VOLATILE"

    def test_trending_states_unchanged(self):
        # BULL 仍需 ≥2 票 + 流入
        eng = MarketRegimeEngine()
        bull = eng.evaluate(
            "SOLUSDT", "up", 100.0, 99.0, 101.0, 99.5, 1.5, 0.1, True,
            btc_trend="up", btc_change_24h=3.0,
        )
        assert bull.regime == "BULL"
        # PANIC 仍需 ≥3% 振幅 + 放量
        panic = eng.evaluate(
            "SOLUSDT", "down", 95.0, 100.0, 110.0, 100.0, 3.0, -0.2, False,
            btc_trend="down", btc_change_24h=-5.0,
        )
        assert panic.regime == "PANIC"

    def test_volatile_disables_grid(self):
        adj = MarketRegimeEngine.strategy_adjustment("VOLATILE")
        assert adj["grid_enabled"] is False
        assert adj["add_position_allowed"] is False
        adj_n = MarketRegimeEngine.strategy_adjustment("NORMAL")
        assert adj_n["grid_enabled"] is True


class TestCoefficientTables6State:
    def test_exposure_has_all_six(self):
        for s in ALL_SIX:
            assert s in EXPOSURE_TABLE

    def test_exposure_monotonic(self):
        # 强牛 > 牛 > 平静 > 震荡 > 宽幅 > 熊 > 恐慌
        chain = ["strong_bull", "BULL", "NORMAL", "SIDEWAY", "VOLATILE", "BEAR", "PANIC"]
        for a, b in zip(chain, chain[1:]):
            assert EXPOSURE_TABLE[a] > EXPOSURE_TABLE[b], (a, b)

    def test_size_factor_has_all_six(self):
        for s in ALL_SIX:
            assert s in REGIME_SIZE_FACTOR

    def test_decision_factors_has_all_six(self):
        for s in ALL_SIX:
            assert s in DecisionEngine.REGIME_BUY_FACTOR
            assert s in DecisionEngine.REGIME_SELL_FACTOR

    def test_decision_buy_factor_monotonic(self):
        # 买系数随风险上升单调下降, PANIC 归零
        chain = ["BULL", "NORMAL", "SIDEWAY", "VOLATILE", "BEAR", "PANIC"]
        for a, b in zip(chain, chain[1:]):
            assert DecisionEngine.REGIME_BUY_FACTOR[a] >= DecisionEngine.REGIME_BUY_FACTOR[b], (a, b)
        assert DecisionEngine.REGIME_BUY_FACTOR["PANIC"] == 0.0

    def test_sizer_respects_volatile(self):
        s = PositionSizer()
        # VOLATILE 买入规模小于 SIDEWAY, 大于 0
        vol = s.size(85, 85, "VOLATILE", 20000.0, 100.0)
        side = s.size(85, 85, "SIDEWAY", 20000.0, 100.0)
        assert 0 <= vol["quote"] < side["quote"]

    def test_allocator_volatile_exposure_midrange(self):
        alloc = PortfolioAllocator()
        e_vol = alloc.target_exposure("VOLATILE", 0.6)
        e_side = alloc.target_exposure("SIDEWAY", 0.6)
        e_bear = alloc.target_exposure("BEAR", 0.6)
        assert e_bear < e_vol < e_side

    def test_volatile_not_blocking_adds(self):
        """VOLATILE 是中性高波(非下跌), 不进入停止加仓档"""
        alloc = PortfolioAllocator()
        plan = alloc.plan(
            "SOLUSDT", "VOLATILE", 0.6, 20000.0, 100.0,
            current_core_qty=1.0, current_trade_qty=0.0,
        )
        assert plan.regime == "VOLATILE"
        assert not any("停止加仓" in r for r in plan.reasons)
