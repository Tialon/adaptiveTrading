"""V10.5: REDUCE_ONLY(卖出不得超持仓)测试

现货多头卖出最终闸门: 执行前重读持仓封顶, 无持仓拒绝, 关掉风控审批到
执行之间的竞态窗口。订单落 reduce_only 标记(仅 SELL)。
"""

import pytest
from sqlalchemy import func, select

from at01_common.database import AsyncSessionLocal
from at01_common.models import Order
from at50_execution.execution_executor import ExecutionEngine
from at50_strategy.strategy_base import Signal, SignalSide
from at60_risk.risk_manager import RiskManager


def _make_signal(**kw) -> Signal:
    base = dict(symbol="SOLUSDT", strategy="grid", side=SignalSide.BUY,
                price=100.0, quantity=1.0)
    base.update(kw)
    return Signal(**base)


def _engine_with_position(qty: float) -> ExecutionEngine:
    rm = RiskManager()
    rm.positions.apply_buy("SOLUSDT", qty, 100.0)
    return ExecutionEngine(risk_manager=rm)


class TestReduceOnly:
    async def test_sell_no_position_rejected(self, db_tables):
        engine = ExecutionEngine(risk_manager=RiskManager())
        sig = _make_signal(side=SignalSide.SELL, quantity=1.0)
        result = await engine.execute(sig)
        assert result is None
        async with AsyncSessionLocal() as session:
            cnt = (await session.execute(
                select(func.count()).select_from(Order)
            )).scalar()
        assert cnt == 0  # 无持仓拒绝 -> 不落订单

    async def test_sell_capped_to_position(self, db_tables):
        engine = _engine_with_position(1.0)  # 持仓 1.0
        sig = _make_signal(side=SignalSide.SELL, quantity=2.0)
        result = await engine.execute(sig)
        assert result is not None
        assert result["fill_qty"] == pytest.approx(1.0)  # 封顶到持仓
        async with AsyncSessionLocal() as session:
            o = (await session.execute(
                select(Order).where(Order.client_order_id == result["client_order_id"])
            )).scalar_one()
        assert o.quantity == pytest.approx(1.0)
        assert o.reduce_only is True

    async def test_buy_not_reduce_only(self, db_tables):
        engine = ExecutionEngine(risk_manager=RiskManager())
        sig = _make_signal(side=SignalSide.BUY, quantity=1.0)
        result = await engine.execute(sig)
        assert result is not None
        async with AsyncSessionLocal() as session:
            o = (await session.execute(
                select(Order).where(Order.client_order_id == result["client_order_id"])
            )).scalar_one()
        assert o.reduce_only is False
