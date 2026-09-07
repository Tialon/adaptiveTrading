"""V10.5: 显式风险状态机(RiskStateMachine)测试

NORMAL/PAUSED/KILLED 三态 + 时间窗自动恢复 + 迁移告警去重 + RiskEvent 审计。
"""

import time

from at60_risk.risk_manager import RiskManager
from at60_risk.risk_state import RiskState, RiskStateMachine


class TestRiskStateMachine:
    def test_initial_normal(self):
        sm = RiskStateMachine(pause_seconds=10.0)
        assert sm.state is RiskState.NORMAL
        assert sm.can_trade()
        assert sm.block_reason() == ""

    def test_pause_transitions_and_blocks(self):
        sm = RiskStateMachine(pause_seconds=10.0)
        assert sm.pause("价格瞬间波动") is True
        assert sm.state is RiskState.PAUSED
        assert sm.is_paused()
        assert not sm.can_trade()
        assert "价格瞬间波动" in sm.block_reason()

    def test_repause_same_reason_no_transition(self):
        sm = RiskStateMachine(pause_seconds=10.0)
        assert sm.pause("行情静默") is True
        assert sm.pause("行情静默") is False  # 同因续期不重复告警
        assert sm.state is RiskState.PAUSED

    def test_repause_new_reason_is_transition(self):
        sm = RiskStateMachine(pause_seconds=10.0)
        assert sm.pause("行情静默") is True
        assert sm.pause("快速暴跌") is True  # 新原因 -> 状态切换
        assert "快速暴跌" in sm.reason

    def test_auto_recover_after_window(self, monkeypatch):
        sm = RiskStateMachine(pause_seconds=5.0)
        sm.pause("测试")
        assert sm.is_paused()
        orig = time.time
        monkeypatch.setattr(time, "time", lambda: orig() + 6.0)
        assert not sm.is_paused()
        assert sm.state is RiskState.NORMAL
        assert sm.can_trade()

    def test_kill_overrides_and_blocks_pause(self):
        sm = RiskStateMachine(pause_seconds=5.0)
        sm.kill("启动对账未通过")
        assert sm.is_killed()
        assert not sm.can_trade()
        assert "急停" in sm.block_reason()
        # 急停优先, 暂停不再降级
        assert sm.pause("行情静默") is False
        assert sm.is_killed()

    def test_reset_from_killed(self):
        # V10.7: reset 不再直接回 NORMAL, 而是进入 RECOVERY_CHECK(禁止裸 reset)
        sm = RiskStateMachine(pause_seconds=5.0)
        sm.kill("人工急停")
        sm.reset()
        assert sm.state is RiskState.RECOVERY_CHECK
        assert not sm.can_trade()  # 恢复核验中仍不可交易
        sm.confirm_recovered()
        assert sm.state is RiskState.NORMAL
        assert sm.can_trade()

    def test_recover_only_from_paused(self):
        sm = RiskStateMachine(pause_seconds=5.0)
        sm.pause("测试")
        sm.recover()
        assert sm.state is RiskState.NORMAL
        # KILLED 不被 recover 覆盖
        sm.kill("急停")
        sm.recover()
        assert sm.is_killed()

    def test_status(self):
        sm = RiskStateMachine(pause_seconds=5.0)
        sm.pause("测试")
        assert sm.status() == {"state": "PAUSED", "reason": "测试"}


class TestRiskManagerIntegration:
    def test_pause_records_state_event(self, monkeypatch):
        rm = RiskManager()
        calls = []
        monkeypatch.setattr(
            rm, "_record_event_now",
            lambda et, detail, equity=None: calls.append((et, detail)),
        )
        rm._pause("测试异常")
        assert ("risk_state", "PAUSED: 测试异常") in calls

    def test_pause_dedup_no_duplicate_event(self, monkeypatch):
        rm = RiskManager()
        calls = []
        monkeypatch.setattr(
            rm, "_record_event_now",
            lambda et, detail, equity=None: calls.append((et, detail)),
        )
        rm._pause("行情静默")
        rm._pause("行情静默")
        assert len(calls) == 1  # 同因续期不重复落事件

    def test_can_trade_and_block_reason(self):
        rm = RiskManager()
        assert rm.can_trade()
        rm._pause("价格瞬间波动")
        assert not rm.can_trade()
        assert rm.anomaly_paused
        assert "异常保护" in rm.block_reason

    def test_status_includes_risk_state(self):
        rm = RiskManager()
        rm._pause("测试")
        st = rm.status()
        assert "risk_state" in st
        assert st["risk_state"]["state"] == "PAUSED"
