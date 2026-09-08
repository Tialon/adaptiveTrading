"""V11.4 P0-2 恢复/重启长跑仿真(kill switch / recovery / restart / recovery-then-trade)。

无人值守系统崩溃/急停后必须安全恢复且账务不漂移:
- 急停(kill switch): armed -> 禁开禁减, 财务不变量仍成立(冻结不改账);
- 恢复两步(RESET -> RECOVERY_CHECK -> confirm_recovered)+ 人工 disarm: 全程门禁正确;
- 重启: 新 RiskManager 经 load_from_db 恢复急停冻结(持久化 id=1);
- 恢复后交易(recovery-then-trade): 解除冻结后继续多周期交易, 不变量成立。
"""


from at60_risk.risk_manager import RiskManager
from at60_risk.risk_state import RiskState
from tests.long_running._harness import (
    _paper_engine,
    _signal,
    assert_financial_invariants,
)

SYMBOL = "SOLUSDT"


async def _round_trip(engine, rm, clock, price: float) -> None:
    r = await engine.execute(_signal("BUY", 1.0, price))
    assert r is not None and r["status"] == "FILLED", r
    clock.advance(100.0)
    r = await engine.execute(_signal("SELL", 1.0, price * 1.02))
    assert r is not None and r["status"] == "FILLED", r
    clock.advance(100.0)
    await assert_financial_invariants(engine, rm, price * 1.02)


class TestKillSwitchSoak:
    async def test_kill_switch_blocks_and_invariants_hold(self, db_tables, clock):
        """急停武装后禁开禁减; 冻结不改账(不变量仍成立)。"""
        rm = RiskManager()
        engine = _paper_engine(rm)
        await _round_trip(engine, rm, clock, 100.0)

        rm.kill_switch.arm("权益漂移")
        rm.state_machine.kill("权益漂移")
        assert not rm.can_buy()
        assert not rm.can_sell()
        assert not rm.can_trade()

        # 急停冻结不改账: 已成交的账务不变量仍成立
        await assert_financial_invariants(engine, rm, 102.0)


class TestRecoveryTwoStepSoak:
    async def test_recovery_two_step_resumes_trading_invariants_hold(self, db_tables, clock):
        """kill -> reset -> confirm_recovered -> disarm -> 恢复交易, 不变量成立。"""
        rm = RiskManager()
        engine = _paper_engine(rm)
        await _round_trip(engine, rm, clock, 100.0)

        rm.kill_switch.arm("权益漂移")
        rm.state_machine.kill("权益漂移")
        assert not rm.can_buy()

        # 裸 reset -> RECOVERY_CHECK: 仍禁(不得裸恢复)
        rm.state_machine.reset()
        assert rm.state_machine.current == "RECOVERY_CHECK"
        assert not rm.can_buy()

        # 对账确认 + 人工 disarm -> 完全恢复
        rm.state_machine.confirm_recovered()
        assert rm.state_machine.state is RiskState.NORMAL
        assert not rm.can_buy()  # kill_switch 仍 armed
        rm.kill_switch.disarm()
        assert rm.can_buy() and rm.can_sell() and rm.can_trade()

        # 恢复后继续交易, 不变量持续成立
        for price in (103.0, 104.0, 105.0):
            await _round_trip(engine, rm, clock, price)

    async def test_recover_does_not_skip_recovery_check(self, db_tables):
        """recover() 只能救 PAUSED/REDUCE_ONLY, 不能跳过 RECOVERY_CHECK 确认。"""
        rm = RiskManager()
        rm.state_machine.kill("急停")
        rm.state_machine.reset()
        rm.state_machine.recover()  # 无副作用
        assert rm.state_machine.current == "RECOVERY_CHECK"
        assert not rm.can_buy()


class TestRestartSoak:
    async def test_restart_preserves_kill_switch_freeze(self, db_tables, clock):
        """急停持久化 -> 重启(新 RiskManager + load_from_db)后仍冻结交易。"""
        rm1 = RiskManager()
        rm1.kill_switch.arm("对账漂移")
        await rm1.kill_switch.persist()

        rm2 = RiskManager()
        await rm2.kill_switch.load_from_db()
        assert rm2.kill_switch.is_armed
        assert rm2.kill_switch.reason == "对账漂移"
        # 关键安全不变量: 即使状态机回 NORMAL, 急停开关仍兜底禁开/禁减
        assert rm2.state_machine.state is RiskState.NORMAL
        assert not rm2.can_buy()
        assert not rm2.can_sell()
        assert not rm2.can_trade()

    async def test_restart_then_recover_unblocks(self, db_tables):
        """重启后人工 disarm 即可恢复交易。"""
        rm1 = RiskManager()
        rm1.kill_switch.arm("对账漂移")
        await rm1.kill_switch.persist()

        rm2 = RiskManager()
        await rm2.kill_switch.load_from_db()
        assert not rm2.can_buy()
        rm2.kill_switch.disarm()
        await rm2.kill_switch.persist()
        assert rm2.can_buy() and rm2.can_sell() and rm2.can_trade()

    async def test_recover_disarm_persisted_no_flashback(self, db_tables):
        """recover 的 disarm 落库, 二次重启后不闪回冻结。"""
        rm = RiskManager()
        rm.kill_switch.arm("急停")
        await rm.kill_switch.persist()
        rm.kill_switch.disarm()
        await rm.kill_switch.persist()

        rm3 = RiskManager()
        await rm3.kill_switch.load_from_db()
        assert not rm3.kill_switch.is_armed
        assert rm3.can_buy()


class TestRecoveryThenTradeSoak:
    async def test_recovery_then_trade_invariants_hold(self, db_tables, clock):
        """完整闭环: 交易 -> 急停 -> 恢复 -> 再交易, 全程不变量成立。"""
        rm = RiskManager()
        engine = _paper_engine(rm)

        # 阶段 1: 正常交易
        await _round_trip(engine, rm, clock, 100.0)
        await _round_trip(engine, rm, clock, 101.0)

        # 阶段 2: 急停(权益漂移)
        rm.kill_switch.arm("权益漂移")
        rm.state_machine.kill("权益漂移")
        assert not rm.can_buy()
        await assert_financial_invariants(engine, rm, 102.0)

        # 阶段 3: 恢复两步 + 人工 disarm
        rm.state_machine.reset()
        rm.state_machine.confirm_recovered()
        rm.kill_switch.disarm()
        assert rm.can_buy()

        # 阶段 4: 恢复后再交易, 不变量持续成立
        await _round_trip(engine, rm, clock, 103.0)
        await _round_trip(engine, rm, clock, 104.0)
        await assert_financial_invariants(engine, rm, 104.0 * 1.02)

    async def test_repeated_kill_recover_cycles_idempotent(self, db_tables):
        """反复 kill/recover 不污染状态、不残留 reason, 每次恢复后皆可交易。"""
        rm = RiskManager()
        for i in range(10):
            rm.kill_switch.arm(f"急停{i}")
            rm.state_machine.kill(f"急停{i}")
            assert not rm.can_buy()
            rm.state_machine.reset()
            rm.state_machine.confirm_recovered()
            rm.kill_switch.disarm()
            assert rm.state_machine.state is RiskState.NORMAL
            assert rm.state_machine.reason == ""
            assert rm.block_reason == ""
            assert rm.can_buy() and rm.can_sell() and rm.can_trade()
