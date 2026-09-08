"""V11.3 P0-5 Recovery 状态机压力审计(restart-oriented)。

无人值守系统长期运行, 崩溃/重启后安全状态必须保持。本片钉住恢复状态机的重启语义:

- 急停(KillSwitch)持久化到 kill_switch_state(单行 id=1); 重启后新 RiskManager 经
  `load_from_db()` 恢复冻结, 即使 RiskStateMachine 为纯内存(重启回 NORMAL), 持久化
  急停开关仍是「禁开仓 + 禁减仓」的安全兜底。
- 恢复两步(RESET -> RECOVERY_CHECK -> confirm_recovered)+ 人工 disarm 全程门禁正确,
  KILLED 不得裸 reset 到可交易。
- 反复 kill/recover 循环幂等、不污染状态。
- PAUSED / REDUCE_ONLY 为内存态(重启清除) —— 已知权衡, 本片显式钉死, 防止后续改动
  误以为它们会被持久化。
"""

import asyncio


from at60_risk.risk_manager import RiskManager
from at60_risk.risk_state import RiskState, RiskStateMachine


class TestRestartPreservesKillSwitch:
    async def test_restart_preserves_kill_switch_block(self, db_tables):
        """急停持久化 -> 重启(新 RiskManager + load_from_db)后仍冻结交易。"""
        rm1 = RiskManager()
        rm1.kill_switch.arm("对账漂移")
        await rm1.kill_switch.persist()

        # 模拟重启: 全新 RiskManager(状态机为内存态, 自动回 NORMAL), 仅加载急停开关
        rm2 = RiskManager()
        await rm2.kill_switch.load_from_db()

        assert rm2.kill_switch.is_armed
        assert rm2.kill_switch.reason == "对账漂移"
        # 关键安全不变量: 即使状态机回 NORMAL, 急停开关仍兜底禁开仓/禁减仓
        assert rm2.state_machine.state is RiskState.NORMAL
        assert not rm2.can_buy()
        assert not rm2.can_sell()
        assert not rm2.can_trade()

    async def test_restart_then_recover_unblocks(self, db_tables):
        """重启后人工 recover(disarm)即可恢复交易。"""
        rm1 = RiskManager()
        rm1.kill_switch.arm("对账漂移")
        await rm1.kill_switch.persist()

        rm2 = RiskManager()
        await rm2.kill_switch.load_from_db()
        assert not rm2.can_buy()

        rm2.kill_switch.disarm()
        await rm2.kill_switch.persist()
        assert rm2.can_buy()
        assert rm2.can_sell()
        assert rm2.can_trade()

    async def test_recover_disarm_is_persisted(self, db_tables):
        """recover 的 disarm 落库, 二次重启后仍为已解除(不闪回冻结)。"""
        rm = RiskManager()
        rm.kill_switch.arm("急停")
        await rm.kill_switch.persist()
        rm.kill_switch.disarm()
        await rm.kill_switch.persist()

        rm3 = RiskManager()
        await rm3.kill_switch.load_from_db()
        assert not rm3.kill_switch.is_armed
        assert rm3.can_buy()


class TestRecoveryTwoStep:
    def test_killed_blocks_buy_and_sell_at_each_stage(self):
        """KILLED 全程门禁: reset 前 / RECOVERY_CHECK 中 / confirm 后(被急停兜底)。"""
        rm = RiskManager()
        rm.kill_switch.arm("权益漂移")
        rm.state_machine.kill("权益漂移")

        # KILLED: 禁买禁卖, kill_switch 优先给出「急停中」
        assert not rm.can_buy()
        assert not rm.can_sell()
        assert "急停" in rm.block_reason

        # 裸 reset -> RECOVERY_CHECK: 仍禁买禁卖(禁止裸 reset 到可交易)
        rm.state_machine.reset()
        assert rm.state_machine.current == "RECOVERY_CHECK"
        assert not rm.can_buy()
        assert not rm.can_sell()
        # 状态机层面报「恢复核验中」; 管理器层面因 kill_switch 仍 armed 优先报「急停中」
        assert "恢复核验中" in rm.state_machine.block_reason()
        assert "急停" in rm.block_reason

        # confirm_recovered -> NORMAL, 但急停开关仍兜底禁交易
        rm.state_machine.confirm_recovered()
        assert rm.state_machine.state is RiskState.NORMAL
        assert not rm.can_buy()  # kill_switch 仍 armed
        assert not rm.can_sell()

        # 人工 disarm -> 完全恢复
        rm.kill_switch.disarm()
        assert rm.can_buy()
        assert rm.can_sell()
        assert rm.can_trade()

    def test_recovery_check_block_reason_without_killswitch(self):
        """纯状态机 RECOVERY_CHECK(无 kill_switch)时, 管理器 block_reason 也报「恢复核验中」。"""
        rm = RiskManager()
        rm.state_machine.kill("急停")
        rm.state_machine.reset()
        assert rm.state_machine.current == "RECOVERY_CHECK"
        assert not rm.can_buy() and not rm.can_sell()
        assert "恢复核验中" in rm.block_reason

    def test_recover_does_not_escape_recovery_check(self):
        """recover() 只能救 PAUSED/REDUCE_ONLY, 不能跳过 RECOVERY_CHECK 确认。"""
        rm = RiskManager()
        rm.state_machine.kill("急停")
        rm.state_machine.reset()
        rm.state_machine.recover()  # 无副作用
        assert rm.state_machine.current == "RECOVERY_CHECK"


class TestRepeatedCycles:
    def test_repeated_kill_recover_cycles_are_idempotent(self):
        """反复 kill/recover 不污染状态、无残留 reason/计数器。"""
        rm = RiskManager()
        for i in range(10):
            rm.kill_switch.arm(f"急停{i}")
            rm.state_machine.kill(f"急停{i}")
            assert not rm.can_buy()
            assert not rm.can_sell()

            rm.state_machine.reset()
            rm.state_machine.confirm_recovered()
            rm.kill_switch.disarm()

            assert rm.state_machine.state is RiskState.NORMAL
            assert rm.state_machine.reason == ""
            assert rm.block_reason == ""
            assert rm.can_buy() and rm.can_sell() and rm.can_trade()


class TestMemoryOnlyStatesRestartSemantics:
    def test_pause_auto_recovers_after_window(self):
        """PAUSED 时间窗到期自动回 NORMAL(无人工干预)。"""
        sm = RiskStateMachine(pause_seconds=0.02)
        sm.pause("行情静默")
        assert sm.state is RiskState.PAUSED
        assert not sm.can_buy()
        asyncio.run(asyncio.sleep(0.05))
        assert sm.state is RiskState.NORMAL
        assert sm.can_buy()

    def test_reduce_only_allows_sell_not_buy_and_recovers(self):
        """REDUCE_ONLY: 禁开新仓、保留卖出; recover() 回 NORMAL。"""
        rm = RiskManager()
        rm.state_machine.reduce_only("回撤")
        assert not rm.can_buy()
        assert rm.can_sell()
        rm.state_machine.recover()
        assert rm.state_machine.state is RiskState.NORMAL
        assert rm.can_buy()

    def test_memory_only_states_cleared_on_restart(self):
        """显式钉死已知权衡: PAUSED/REDUCE_ONLY 为内存态, 重启清除(不持久化)。"""
        rm1 = RiskManager()
        rm1.state_machine.reduce_only("回撤")
        rm1.state_machine.pause("行情静默")
        assert not rm1.can_buy()

        # 重启: 新 RiskManager 的内存状态机回 NORMAL
        rm2 = RiskManager()
        assert rm2.state_machine.state is RiskState.NORMAL
        # 若没有持久化急停开关兜底, 此处即为可交易(与急停持久化形成对照)
        assert rm2.can_buy()
