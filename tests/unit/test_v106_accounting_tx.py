"""V10.6: 成交后本地记账强一致事务 + RECOVERY_REQUIRED 测试

验证:
- 成交后 Position / PositionLot / AccountLedger 在单个事务内原子提交;
- 任一写失败 -> 整体回滚(不留部分镜像), Order.accounting_state 置
  RECOVERY_REQUIRED, 急停冻结;
- 成功路径 accounting_state 保持 OK, 不冻结。
"""

from sqlalchemy import func, select

from at01_common.database import AsyncSessionLocal
from at01_common.models import AccountLedger, Order, Position, PositionLot
from at60_execution.execution_executor import ExecutionEngine
from at30_strategy.strategy_base import Signal, SignalSide
from at50_risk.risk_manager import RiskManager


async def _count(model) -> int:
    async with AsyncSessionLocal() as session:
        return (await session.execute(select(func.count()).select_from(model))).scalar()


async def _order(client_order_id: str) -> Order:
    async with AsyncSessionLocal() as session:
        return (
            await session.execute(
                select(Order).where(Order.client_order_id == client_order_id)
            )
        ).scalar_one()


class TestAccountingTransaction:
    async def test_buy_accounting_commits_atomically(self, db_tables):
        rm = RiskManager()
        engine = ExecutionEngine(risk_manager=rm)

        result = await engine.execute(Signal(
            symbol="SOLUSDT", strategy="trend", side=SignalSide.BUY,
            price=100.0, quantity=1.0,
        ))

        assert result is not None
        assert result["status"] == "FILLED"

        # 四表全部落库(原子提交)
        assert await _count(Position) == 1
        assert await _count(PositionLot) == 1
        assert await _count(AccountLedger) == 2  # USDT + SOL

        o = await _order(result["client_order_id"])
        assert o.accounting_state == "OK"

        # 未冻结
        assert rm.kill_switch.is_armed is False

    async def test_accounting_failure_rolls_back_and_flags_recovery(self, db_tables, monkeypatch):
        rm = RiskManager()
        engine = ExecutionEngine(risk_manager=rm)

        async def _boom(**kwargs):
            raise RuntimeError("ledger boom")

        # 让账本写失败(在事务内抛异常) -> 应整体回滚
        monkeypatch.setattr(engine.account_ledger, "record", _boom)

        result = await engine.execute(Signal(
            symbol="SOLUSDT", strategy="trend", side=SignalSide.BUY,
            price=100.0, quantity=1.0,
        ))

        # 返回 RECOVERY_REQUIRED 状态
        assert result is not None
        assert result["status"] == "RECOVERY_REQUIRED"

        # Order 标记 RECOVERY_REQUIRED
        o = await _order(result["client_order_id"])
        assert o.accounting_state == "RECOVERY_REQUIRED"

        # 整体回滚: Position / PositionLot / AccountLedger 均无部分镜像
        assert await _count(Position) == 0
        assert await _count(PositionLot) == 0
        assert await _count(AccountLedger) == 0

        # 急停冻结(不静默漂移)
        assert rm.kill_switch.is_armed is True

    async def test_success_does_not_arm_killswitch(self, db_tables):
        rm = RiskManager()
        engine = ExecutionEngine(risk_manager=rm)

        await engine.execute(Signal(
            symbol="SOLUSDT", strategy="trend", side=SignalSide.BUY,
            price=100.0, quantity=1.0,
        ))
        # 卖出平仓, 走 SELL 记账(SellAllocation + 账本)
        await engine.execute(Signal(
            symbol="SOLUSDT", strategy="trend", side=SignalSide.SELL,
            price=110.0, quantity=1.0,
        ))

        assert rm.kill_switch.is_armed is False
        # 两笔成交 -> 账本 4 行
        assert await _count(AccountLedger) == 4
