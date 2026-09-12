"""V12 主网小资金接管: 风控参数单元测试(§16-19)

- §16: SOL 总敞口硬上限 70%(SOL 市值/权益 ≤ 70%, 超限禁买、放行卖)
- §18: 日内亏损 3% → REDUCE_ONLY(非熔断冷却、非自动清仓)
- §19: 分级回撤 5/8/12/15%(观察/降险/仅减仓/急停)
"""

import pytest

from at01_common.settings import Settings
from at50_risk.risk_manager import RiskManager
from at50_risk.risk_tiered import TIERS
from at30_strategy.strategy_base import Signal, SignalSide


def _cfg(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def make_signal(side=SignalSide.BUY, symbol="SOLUSDT", price=100.0, qty=None, quote=None):
    return Signal(
        symbol=symbol, strategy="test", side=side, price=price,
        quantity=qty, quote_amount=quote, reason=[],
    )


# ---------------------------------------------------------------------------
# §16: SOL 总敞口硬上限
# ---------------------------------------------------------------------------

class TestSolExposureCeiling:
    def test_default_exposure_70pct(self):
        assert _cfg().risk_max_sol_exposure == pytest.approx(0.70)

    def test_exposure_ratio(self):
        rm = RiskManager()
        rm.breaker.current_equity = 100000.0
        rm.positions.apply_buy("SOLUSDT", 700.0, 100.0)  # 70000 = 70%
        assert rm.sol_exposure_ratio({"SOLUSDT": 100.0}) == pytest.approx(0.70)

    async def test_buy_blocked_over_70pct(self):
        """敞口已超 70% -> 买入被拒(隔离 40% 子仓上限, 只测 70% 硬上限)。"""
        rm = RiskManager()
        rm.settings.risk_max_position_pct = 1.0  # 关闭 40% 子仓上限
        rm.breaker.current_equity = 100000.0
        rm.positions.apply_buy("SOLUSDT", 710.0, 100.0)  # 71000 = 71%
        d = await rm.check(make_signal(qty=1.0, price=100.0), {"SOLUSDT": 100.0})
        assert not d.approved
        assert "敞口" in d.reason

    async def test_sell_allowed_over_70pct(self):
        """超限只禁买、放行卖(§16 后半句)。"""
        rm = RiskManager()
        rm.breaker.current_equity = 100000.0
        rm.positions.apply_buy("SOLUSDT", 710.0, 100.0)
        d = await rm.check(
            make_signal(side=SignalSide.SELL, qty=1.0, price=100.0),
            {"SOLUSDT": 100.0},
        )
        assert d.approved


# ---------------------------------------------------------------------------
# §18: 日内亏损 3% → REDUCE_ONLY
# ---------------------------------------------------------------------------

class TestDailyLossReduceOnly:
    def test_default_daily_loss_3pct(self):
        assert _cfg().risk_max_daily_loss == pytest.approx(0.03)

    def test_daily_loss_triggers_reduce_only(self):
        rm = RiskManager()
        rm.breaker._day_start_equity = 100000.0
        rm.positions.apply_buy("SOLUSDT", 1.0, 100.0)
        # equity = 100000 + realized(-4000) + unrealized(0) = 96000, 即 -4% < -3%
        rm.positions.get("SOLUSDT").realized_pnl = -4000.0
        status = rm.update_equity({"SOLUSDT": 100.0})
        assert status["equity"] == pytest.approx(96000.0)
        assert rm.state_machine.is_reduce_only()
        assert not rm.can_buy()
        assert rm.can_sell()
        # 非熔断(breaker 未 trip)、非急停(kill_switch 未 armed)
        assert not rm.breaker.is_open
        assert not rm.kill_switch.is_armed

    def test_small_loss_no_reduce_only(self):
        """-2% 未达 3% 阈值 -> 不触发。"""
        rm = RiskManager()
        rm.breaker._day_start_equity = 100000.0
        rm.positions.apply_buy("SOLUSDT", 1.0, 100.0)
        rm.positions.get("SOLUSDT").realized_pnl = -2000.0  # -2%
        rm.update_equity({"SOLUSDT": 100.0})
        assert not rm.state_machine.is_reduce_only()


# ---------------------------------------------------------------------------
# §19: 分级回撤 5/8/12/15
# ---------------------------------------------------------------------------

class TestDrawdownKill:
    def test_tier_thresholds_51215(self):
        assert [t.threshold for t in TIERS] == pytest.approx([0.05, 0.08, 0.12, 0.15])
        assert [t.name for t in TIERS] == ["observe", "reduce_risk", "reduce_only", "kill"]

    def test_drawdown_15pct_kills(self):
        """回撤 ≥ 15% -> 持久急停(KILL + kill_switch), 非冷却自动复位。"""
        rm = RiskManager()
        rm.breaker._day_start_equity = 100000.0
        rm.positions.apply_buy("SOLUSDT", 1.0, 100.0)
        rm.positions.get("SOLUSDT").realized_pnl = -20000.0  # equity = 80000, 回撤 20%
        status = rm.update_equity({"SOLUSDT": 100.0})
        assert status["drawdown"] >= 0.15
        assert rm.kill_switch.is_armed
        assert rm.state_machine.is_killed()

    def test_drawdown_below_15pct_no_kill(self):
        """回撤 10% 未达 15% -> 不急停。"""
        rm = RiskManager()
        rm.breaker._day_start_equity = 100000.0
        rm.positions.apply_buy("SOLUSDT", 1.0, 100.0)
        rm.positions.get("SOLUSDT").realized_pnl = -10000.0  # 回撤 10%
        rm.update_equity({"SOLUSDT": 100.0})
        assert not rm.kill_switch.is_armed
        assert not rm.state_machine.is_killed()


# ---------------------------------------------------------------------------
# 配置审计: 分级回撤档位严格递增
# ---------------------------------------------------------------------------

class TestDrawdownTierValidation:
    def test_default_passes(self):
        assert _cfg().validate() == []

    def test_unordered_tiers_blocked(self):
        problems = _cfg(
            risk_drawdown_observe_pct=0.20,
            risk_drawdown_reduce_pct=0.10,
            risk_drawdown_pause_pct=0.08,
        ).validate()
        assert any("回撤档位" in p for p in problems)

    def test_exposure_out_of_range_blocked(self):
        problems = _cfg(risk_max_sol_exposure=1.01).validate()
        assert any("risk_max_sol_exposure" in p for p in problems)
