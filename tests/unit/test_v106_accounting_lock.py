"""V10.6: Position/Lot symbol 级 asyncio.Lock 测试

验证:
- 同一 symbol 返回同一把锁, 不同 symbol 各自独立;
- 锁真正串行化临界区(无交错);
- 并发同标的成交(核心仓信号绕过交易状态机)在锁下记账一致。
"""

import asyncio

import pytest
from sqlalchemy import func, select

from at01_common.database import AsyncSessionLocal
from at01_common.models import AccountLedger, PositionLot
from at50_execution.execution_executor import ExecutionEngine
from at50_strategy.strategy_base import Signal, SignalSide
from at60_risk.risk_manager import RiskManager


async def _count(model) -> int:
    async with AsyncSessionLocal() as session:
        return (await session.execute(select(func.count()).select_from(model))).scalar()


class TestAccountingLock:
    def test_same_symbol_shares_lock(self):
        engine = ExecutionEngine(risk_manager=RiskManager())
        assert engine._accounting_lock("SOLUSDT") is engine._accounting_lock("SOLUSDT")
        assert engine._accounting_lock("SOLUSDT") is not engine._accounting_lock("BTCUSDT")

    async def test_lock_serializes_critical_section(self):
        engine = ExecutionEngine(risk_manager=RiskManager())
        lock = engine._accounting_lock("SOLUSDT")
        order: list[tuple[str, int]] = []

        async def critical(i: int):
            async with lock:
                order.append(("enter", i))
                await asyncio.sleep(0.001)
                order.append(("exit", i))

        await asyncio.gather(critical(0), critical(1))
        # 串行: 一次只有一个任务在临界区, 无 (enter0 enter1) 交错
        assert order in (
            [("enter", 0), ("exit", 0), ("enter", 1), ("exit", 1)],
            [("enter", 1), ("exit", 1), ("enter", 0), ("exit", 0)],
        )

    async def test_concurrent_core_buys_accounting_consistent(self, db_tables):
        """核心仓信号绕过交易状态机, 并发同标的买入需在锁下保持一致"""
        rm = RiskManager()
        engine = ExecutionEngine(risk_manager=rm)

        s1 = Signal(symbol="SOLUSDT", strategy="trend", side=SignalSide.BUY,
                    price=100.0, quantity=1.0, bucket="core")
        s2 = Signal(symbol="SOLUSDT", strategy="trend", side=SignalSide.BUY,
                    price=100.0, quantity=0.5, bucket="core")

        results = await asyncio.gather(engine.execute(s1), engine.execute(s2))
        assert all(r is not None and r["status"] == "FILLED" for r in results)

        # 持仓为两笔之和
        assert rm.positions.get("SOLUSDT").quantity == pytest.approx(1.5)
        # 两笔买入各落一个 lot + 账本 2 行
        assert await _count(PositionLot) == 2
        assert await _count(AccountLedger) == 4
        # 无记账失败冻结
        assert rm.kill_switch.is_armed is False
