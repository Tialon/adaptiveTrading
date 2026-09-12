"""V10.6(P1-e): Signal 与 Execution 数量分离测试

验证:
- REDUCE_ONLY 缩量不原地改写 signal.quantity(策略原始意图不可变);
- orders.quantity 落实际提交数量(exec_qty), signals.quantity 落原始意图;
- 常见买入路径同样不改写 signal.quantity。
"""

import pytest
from sqlalchemy import select

from at01_common.database import AsyncSessionLocal
from at01_common.models import Order, Signal as SignalModel
from at60_execution.execution_executor import ExecutionEngine
from at30_strategy.strategy_base import Signal, SignalSide
from at50_risk.risk_manager import RiskManager


def _make_signal(**kw) -> Signal:
    base = dict(symbol="SOLUSDT", strategy="grid", side=SignalSide.BUY,
                price=100.0, quantity=1.0)
    base.update(kw)
    return Signal(**base)


def _engine_with_position(qty: float) -> ExecutionEngine:
    rm = RiskManager()
    rm.positions.apply_buy("SOLUSDT", qty, 100.0)
    return ExecutionEngine(risk_manager=rm)


class TestSignalExecQtySeparation:
    async def test_sell_cap_does_not_mutate_signal(self, db_tables):
        """卖出超持仓缩量: 信号数量保持原始意图, 订单数量落实际提交量"""
        engine = _engine_with_position(1.0)  # 持仓 1.0
        sig = _make_signal(side=SignalSide.SELL, quantity=2.0)
        result = await engine.execute(sig)

        assert result is not None
        assert result["fill_qty"] == pytest.approx(1.0)  # 缩量到持仓
        assert sig.quantity == 2.0  # 信号原始意图不被改写

        async with AsyncSessionLocal() as session:
            o = (await session.execute(
                select(Order).where(Order.client_order_id == result["client_order_id"])
            )).scalar_one()
            s = (await session.execute(
                select(SignalModel).where(SignalModel.id == o.signal_id)
            )).scalar_one()
            assert o.quantity == pytest.approx(1.0)  # orders = 实际提交数量
            assert s.quantity == pytest.approx(2.0)  # signals = 原始意图数量

    async def test_buy_signal_quantity_unchanged(self, db_tables):
        """常见买入路径(无缩量)同样不改写 signal.quantity"""
        engine = ExecutionEngine(risk_manager=RiskManager())
        sig = _make_signal(side=SignalSide.BUY, quantity=1.0)
        result = await engine.execute(sig)

        assert result is not None
        assert sig.quantity == 1.0

        async with AsyncSessionLocal() as session:
            o = (await session.execute(
                select(Order).where(Order.client_order_id == result["client_order_id"])
            )).scalar_one()
            assert o.quantity == pytest.approx(1.0)
