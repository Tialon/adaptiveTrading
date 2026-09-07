"""V10.6(P1-g): 风险状态机 REDUCE_ONLY 态 + can_buy/can_sell 测试

验证:
- REDUCE_ONLY 态: 禁开新仓(can_buy=False)但保留卖出(can_sell=True);
- 不自动恢复(仅 recover/reset 退出), KILLED 优先;
- RiskManager 方向闸门: 仅减仓态下 BUY 被拒、SELL 放行。
"""

import time

import pytest

from at60_risk.risk_manager import RiskManager
from at60_risk.risk_state import RiskState, RiskStateMachine
from at50_strategy.strategy_base import Signal, SignalSide


def _make_signal(**kw) -> Signal:
    base = dict(symbol="SOLUSDT", strategy="grid", side=SignalSide.BUY,
                price=100.0, quantity=1.0)
    base.update(kw)
    return Signal(**base)


class TestReduceOnlyState:
    def test_reduce_only_blocks_buy_allows_sell(self):
        sm = RiskStateMachine(pause_seconds=10.0)
        assert sm.reduce_only("回撤降险") is True
        assert sm.state is RiskState.REDUCE_ONLY
        assert sm.is_reduce_only()
        assert not sm.can_buy()
        assert sm.can_sell()
        assert not sm.can_trade()  # 完全可交易为 False
        assert "仅减仓" in sm.block_reason()

    def test_reduce_only_does_not_auto_recover(self, monkeypatch):
        sm = RiskStateMachine(pause_seconds=5.0)
        sm.reduce_only("降险")
        orig = time.time
        monkeypatch.setattr(time, "time", lambda: orig() + 60.0)
        assert sm.state is RiskState.REDUCE_ONLY  # 无时间窗, 不自动恢复

    def test_recover_from_reduce_only(self):
        sm = RiskStateMachine(pause_seconds=5.0)
        sm.reduce_only("降险")
        sm.recover()
        assert sm.state is RiskState.NORMAL
        assert sm.can_buy() and sm.can_sell()

    def test_kill_overrides_reduce_only(self):
        sm = RiskStateMachine(pause_seconds=5.0)
        sm.reduce_only("降险")
        sm.kill("急停")
        assert sm.is_killed()
        assert sm.reduce_only("再降险") is False  # KILLED 优先
        assert sm.is_killed()

    def test_reduce_only_same_state_no_transition(self):
        sm = RiskStateMachine(pause_seconds=5.0)
        assert sm.reduce_only("降险") is True
        assert sm.reduce_only("降险") is False  # 同态不重复切换


class TestRiskManagerDirectionGate:
    def test_can_buy_can_sell_in_reduce_only(self):
        rm = RiskManager()
        assert rm.can_buy() and rm.can_sell()
        rm.reduce_only("回撤降险")
        assert not rm.can_buy()
        assert rm.can_sell()

    async def test_check_blocks_buy_allows_sell_in_reduce_only(self):
        rm = RiskManager()
        rm.positions.apply_buy("SOLUSDT", 1.0, 100.0)  # 持仓 1.0
        rm.reduce_only("回撤降险")

        buy = await rm.check(_make_signal(side=SignalSide.BUY), {"SOLUSDT": 100.0})
        assert not buy.approved
        assert "仅减仓" in buy.reason

        sell = await rm.check(_make_signal(side=SignalSide.SELL), {"SOLUSDT": 100.0})
        assert sell.approved  # 仅减仓态放行卖出
        assert sell.quantity == pytest.approx(1.0)

    async def test_check_paused_blocks_sell(self):
        rm = RiskManager()
        rm.positions.apply_buy("SOLUSDT", 1.0, 100.0)
        rm.pause("行情静默")  # PAUSED 比 REDUCE_ONLY 更严, 连卖也禁

        sell = await rm.check(_make_signal(side=SignalSide.SELL), {"SOLUSDT": 100.0})
        assert not sell.approved
        assert "异常保护" in sell.reason
