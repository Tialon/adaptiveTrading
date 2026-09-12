"""V10.7(P1-e): 风险状态机 RECOVERY_CHECK(急停解除需两步)测试

验证:
- KILLED -> reset() -> RECOVERY_CHECK(禁止裸 reset 到 NORMAL);
- RECOVERY_CHECK 仍不可交易(block_reason / can_trade / can_buy / can_sell);
- confirm_recovered() -> NORMAL;
- recover() 不逃逸 RECOVERY_CHECK(必须显式 confirm_recovered);
- RECOVERY_CHECK 优先于 PAUSED / REDUCE_ONLY, kill() 可再次覆盖。
"""

from at50_risk.risk_state import RiskState, RiskStateMachine


class TestRecoveryCheck:
    def test_reset_enters_recovery_check_not_normal(self):
        sm = RiskStateMachine(pause_seconds=5.0)
        sm.kill("权益对账漂移")
        sm.reset()
        assert sm.state is RiskState.RECOVERY_CHECK
        assert sm.is_recovery_check()
        assert not sm.is_killed()

    def test_recovery_check_blocks_trading(self):
        sm = RiskStateMachine(pause_seconds=5.0)
        sm.kill("人工急停")
        sm.reset()
        assert not sm.can_trade()
        assert not sm.can_buy()
        assert not sm.can_sell()
        assert "恢复核验中" in sm.block_reason()

    def test_confirm_recovered_returns_normal(self):
        sm = RiskStateMachine(pause_seconds=5.0)
        sm.kill("人工急停")
        sm.reset()
        sm.confirm_recovered()
        assert sm.state is RiskState.NORMAL
        assert sm.can_trade()
        assert sm.block_reason() == ""

    def test_recover_does_not_escape_recovery_check(self):
        sm = RiskStateMachine(pause_seconds=5.0)
        sm.kill("人工急停")
        sm.reset()
        sm.recover()  # recover 只覆盖 PAUSED/REDUCE_ONLY, 不覆盖 RECOVERY_CHECK
        assert sm.state is RiskState.RECOVERY_CHECK

    def test_kill_overrides_recovery_check(self):
        sm = RiskStateMachine(pause_seconds=5.0)
        sm.kill("人工急停")
        sm.reset()
        sm.kill("再次急停")
        assert sm.is_killed()

    def test_reset_non_killed_is_noop(self):
        sm = RiskStateMachine(pause_seconds=5.0)
        sm.pause("行情静默")
        sm.reset()  # 非 KILLED 态调用无副作用
        assert sm.state is RiskState.PAUSED

    def test_confirm_recovered_non_recovery_check_is_noop(self):
        sm = RiskStateMachine(pause_seconds=5.0)
        sm.kill("人工急停")  # 直接 KILLED, 未经 RECOVERY_CHECK
        sm.confirm_recovered()  # 无副作用
        assert sm.is_killed()
