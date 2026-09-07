"""V10: 急停开关 + RiskManager 集成测试

验证: 急停不自动复位(区别于 CircuitBreaker 的 cooldown)、持久化单行 upsert、
加载恢复、以及 RiskManager 统一闸门对急停的短路。
"""

import pytest
from sqlalchemy import select

from at01_common.models import KillSwitchState
from at50_strategy.strategy_base import Signal, SignalSide
from at60_risk.risk_killswitch import KillSwitch
from at60_risk.risk_manager import RiskManager


def make_signal(side=SignalSide.BUY, symbol="BTCUSDT", price=100.0, qty=1.0):
    return Signal(symbol=symbol, strategy="test", side=side, price=price, quantity=qty, reason=[])


class TestKillSwitch:
    def test_default_disarmed(self):
        ks = KillSwitch()
        assert not ks.is_armed
        assert ks.reason == ""
        assert ks.status() == {"armed": False, "reason": ""}

    def test_arm(self):
        ks = KillSwitch()
        ks.arm("测试急停")
        assert ks.is_armed
        assert ks.reason == "测试急停"
        assert ks.status()["armed"] is True

    def test_disarm(self):
        ks = KillSwitch()
        ks.arm("测试急停")
        ks.disarm()
        assert not ks.is_armed
        assert ks.reason == ""

    def test_no_auto_reset(self):
        """急停不自动复位: 与 CircuitBreaker 的 cooldown 自动复位截然不同"""
        ks = KillSwitch()
        ks.arm("急停")
        # 无任何时间推进也会保持冻结
        assert ks.is_armed

    async def test_persist_and_reload(self, db_tables):
        from at01_common.database import AsyncSessionLocal

        ks = KillSwitch()
        ks.arm("启动对账未通过")
        await ks.persist()

        async with AsyncSessionLocal() as session:
            rows = list((await session.execute(select(KillSwitchState))).scalars().all())
        assert len(rows) == 1
        assert rows[0].armed is True
        assert rows[0].reason == "启动对账未通过"

        # 新实例从 DB 恢复冻结状态(重启后仍冻结)
        ks2 = KillSwitch()
        await ks2.load_from_db()
        assert ks2.is_armed
        assert ks2.reason == "启动对账未通过"

    async def test_persist_disarm_upserts_single_row(self, db_tables):
        from at01_common.database import AsyncSessionLocal

        ks = KillSwitch()
        ks.arm("人工急停")
        await ks.persist()
        ks.disarm()
        await ks.persist()

        async with AsyncSessionLocal() as session:
            rows = list((await session.execute(select(KillSwitchState))).scalars().all())
        assert len(rows) == 1  # upsert, 不新增第二行
        assert rows[0].armed is False
        assert rows[0].reason == ""

    async def test_load_empty_db_no_crash(self, db_tables):
        ks = KillSwitch()
        await ks.load_from_db()  # 无行 -> 保持默认, 不抛
        assert not ks.is_armed

    async def test_persist_returns_true_on_success(self, db_tables):
        # 回归: persist 不再静默吞错, 成功返回 True(调用方据此判定急停态是否持久化成功)
        ks = KillSwitch()
        ks.arm("持久化契约")
        assert await ks.persist() is True


class TestRiskManagerKillSwitch:
    async def test_armed_blocks_trading(self):
        rm = RiskManager()
        rm.kill_switch.arm("急停")
        assert not rm.can_trade()
        assert "急停" in rm.block_reason

    async def test_armed_rejects_signal(self):
        rm = RiskManager()
        rm.kill_switch.arm("急停")
        sig = make_signal(qty=1.0)
        d = await rm.check(sig, {"BTCUSDT": 100.0})
        assert not d.approved
        assert "急停" in d.reason

    async def test_armed_rejects_sell_signal(self):
        # 回归: 急停下 SELL 减仓亦被拒(can_sell 短路), 核心仓 REDUCE 不得绕过闸门
        rm = RiskManager()
        rm.kill_switch.arm("急停")
        sig = make_signal(side=SignalSide.SELL, qty=1.0)
        d = await rm.check(sig, {"BTCUSDT": 100.0})
        assert not d.approved
        assert "急停" in d.reason

    def test_reduce_only_allows_sell_not_buy(self):
        # 方向闸门语义: REDUCE_ONLY 下仍可卖出减仓, 但禁开新仓
        rm = RiskManager()
        rm.state_machine.reduce_only("测试仅减仓")
        assert not rm.can_buy()
        assert rm.can_sell()

    async def test_disarmed_allows_trading(self):
        rm = RiskManager()
        rm.kill_switch.arm("急停")
        rm.kill_switch.disarm()
        assert rm.can_trade()
        sig = make_signal(qty=1.0)
        d = await rm.check(sig, {"BTCUSDT": 100.0})
        assert d.approved

    def test_status_includes_kill_switch(self):
        rm = RiskManager()
        rm.kill_switch.arm("急停")
        status = rm.status()
        assert status["kill_switch"]["armed"] is True
        assert status["kill_switch"]["reason"] == "急停"
