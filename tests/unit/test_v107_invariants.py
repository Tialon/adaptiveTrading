"""V10.7(P1-f): 10 个核心不变量测试(跨维度一致性回归护栏)

把贯穿「订单→成交→账本→持仓→风控」链路的核心正确性性质收敛到一处,
作为后续重构/恢复/混沌工程的回归契约。每条不变量对应一条独立断言:

1. 订单意图幂等     —— 同一 idempotency_key 只注册一次订单意图(DB 唯一键);
2. 成交明细幂等     —— 同一 fill_idempotency_key 只落一次成交明细(重复成交不重复记账);
3. 持仓守恒         —— BUY 1.0 后 SELL 1.0, 持仓精确归零(无残留/无负仓);
4. lot 总和 == 持仓 —— FIFO 开仓批次剩余量之和 == PositionState.quantity;
5. 账本持仓行守恒   —— Σ AccountLedger.base 变更 == 当前持仓量;
6. 记账原子性       —— 任一写失败整体回滚(Position/PositionLot/Ledger 无部分镜像);
7. 成交覆盖         —— Σ OrderFill.quantity == Order.filled_quantity;
8. FIFO 已实现盈亏守恒 —— realized == Σ SellAllocation.realized_pnl - 卖出手续费;
9. REDUCE_ONLY 不变量 —— 卖出数量永不超持仓(裸卖空防护), 持仓永不转负;
10. 急停持久化      —— 急停 arm + persist 后, 新实例 load_from_db 仍冻结并关闭所有闸门。
"""

import pytest
from sqlalchemy import func, select

from at01_common.database import AsyncSessionLocal
from at01_common.models import AccountLedger, Order, OrderFill, Position, PositionLot
from at50_execution.execution_executor import ExecutionEngine
from at50_strategy.strategy_base import Signal, SignalSide
from at60_risk.risk_lot import LotTracker
from at60_risk.risk_manager import RiskManager

SYMBOL = "SOLUSDT"


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


async def _sum_column(model, column, where) -> float:
    async with AsyncSessionLocal() as session:
        q = select(func.coalesce(func.sum(column), 0.0)).where(where)
        return (await session.execute(q)).scalar()


def _signal(side: str, qty: float, price: float) -> Signal:
    return Signal(symbol=SYMBOL, strategy="trend", side=SignalSide(side),
                  price=price, quantity=qty)


class TestInvariants:
    # 1. 订单意图幂等
    async def test_1_order_intent_idempotent(self, db_tables):
        engine = ExecutionEngine(risk_manager=RiskManager())
        sig = _signal("BUY", 1.0, 100.0)
        assert await engine._register_intent(sig, "key-dup") is True
        # 同键第二次注册被 DB 唯一键拦截
        assert await engine._register_intent(sig, "key-dup") is False

    # 2. 成交明细幂等
    async def test_2_fill_ingest_idempotent(self, db_tables):
        engine = ExecutionEngine(risk_manager=RiskManager())
        fill = {"id": 42, "orderId": "100", "price": "100.0", "qty": "1.0",
                "quoteQty": "100.0", "commission": "0", "commissionAsset": "USDT",
                "time": 1700000000000}
        args = dict(order_id=None, client_order_id="cid-1", exchange_order_id="100",
                    symbol=SYMBOL, side="BUY")
        assert await engine._record_fills(fills=[fill], **args) == 1
        assert await engine._record_fills(fills=[fill], **args) == 0
        assert await _count(OrderFill) == 1

    # 3. 持仓守恒(买卖往返精确归零)
    async def test_3_position_round_trip_conservation(self, db_tables):
        rm = RiskManager()
        engine = ExecutionEngine(risk_manager=rm)
        await engine.execute(_signal("BUY", 1.0, 100.0))
        assert rm.positions.get(SYMBOL).quantity == pytest.approx(1.0)
        await engine.execute(_signal("SELL", 1.0, 110.0))
        assert rm.positions.get(SYMBOL).quantity == pytest.approx(0.0)

    # 4. lot 总和 == 持仓量
    async def test_4_lot_sum_equals_position(self, db_tables):
        rm = RiskManager()
        engine = ExecutionEngine(risk_manager=rm)
        await engine.execute(_signal("BUY", 2.0, 100.0))
        # FIFO 开仓批次剩余量 == PositionState.quantity(对账不变量)
        assert engine.lot_tracker.reconcile(
            SYMBOL, rm.positions.get(SYMBOL).quantity
        ) is None

    # 5. 账本持仓行守恒(Σ base 变更 == 当前持仓)
    async def test_5_ledger_position_row_conservation(self, db_tables):
        rm = RiskManager()
        engine = ExecutionEngine(risk_manager=rm)
        await engine.execute(_signal("BUY", 1.0, 100.0))
        sol_change = await _sum_column(
            AccountLedger, AccountLedger.change_amount,
            AccountLedger.asset == "SOL",
        )
        assert sol_change == pytest.approx(1.0)
        assert sol_change == pytest.approx(rm.positions.get(SYMBOL).quantity)

    # 6. 记账原子性(任一写失败 -> 整体回滚, 无部分镜像)
    async def test_6_accounting_atomicity(self, db_tables, monkeypatch):
        rm = RiskManager()
        engine = ExecutionEngine(risk_manager=rm)

        async def _boom(*args, **kwargs):
            raise RuntimeError("position persist boom")

        # 让 Position 镜像写失败(事务内第一步) -> 后续 lot/ledger 均不应落库
        monkeypatch.setattr(engine.risk.positions, "persist", _boom)

        result = await engine.execute(_signal("BUY", 1.0, 100.0))
        assert result is not None
        assert result["status"] == "RECOVERY_REQUIRED"

        o = await _order(result["client_order_id"])
        assert o.accounting_state == "RECOVERY_REQUIRED"

        # 整体回滚: 四表均无部分镜像
        assert await _count(Position) == 0
        assert await _count(PositionLot) == 0
        assert await _count(AccountLedger) == 0

        # 急停冻结(不静默漂移)
        assert rm.kill_switch.is_armed is True

    # 7. 成交覆盖(Σ 成交明细 == 订单 filled_quantity)
    async def test_7_fill_coverage(self, db_tables):
        engine = ExecutionEngine(risk_manager=RiskManager())
        result = await engine.execute(_signal("BUY", 1.0, 100.0))
        o = await _order(result["client_order_id"])
        fill_sum = await _sum_column(
            OrderFill, OrderFill.quantity,
            OrderFill.client_order_id == result["client_order_id"],
        )
        assert o.filled_quantity == pytest.approx(1.0)
        assert fill_sum == pytest.approx(o.filled_quantity)

    # 8. FIFO 已实现盈亏守恒(realized == Σ allocation realized_pnl - 卖出费)
    async def test_8_fifo_realized_conservation(self, db_tables):
        lt = LotTracker()
        await lt.add_buy(SYMBOL, 2.0, 100.0)
        await lt.add_buy(SYMBOL, 1.0, 120.0)
        fee = 2.0
        realized, _, allocs = await lt.allocate_sell(SYMBOL, 1.5, 130.0, fee_quote=fee)
        alloc_sum = sum(a["realized_pnl"] for a in allocs)
        assert realized == pytest.approx(alloc_sum - fee)
        # 显式核算: 1.5 全部消费首个 lot(100 成本), (130-100)*1.5 = 45, 减卖出费 2 = 43
        assert realized == pytest.approx(45.0 - fee)

    # 9. REDUCE_ONLY 不变量(卖出永不超持仓, 持仓永不转负)
    async def test_9_reduce_only_no_naked_short(self, db_tables):
        rm = RiskManager()
        engine = ExecutionEngine(risk_manager=rm)
        await engine.execute(_signal("BUY", 1.0, 100.0))
        # 卖出 2.0 但仅持仓 1.0 -> 缩量至 1.0, 不裸卖空
        result = await engine.execute(_signal("SELL", 2.0, 110.0))
        assert result["fill_qty"] == pytest.approx(1.0)
        assert rm.positions.get(SYMBOL).quantity >= 0.0
        assert rm.positions.get(SYMBOL).quantity == pytest.approx(0.0)

    # 10. 急停持久化(重启后仍冻结并关闭所有闸门)
    async def test_10_killswitch_persistence(self, db_tables):
        rm = RiskManager()
        rm.kill_switch.arm("权益对账漂移")
        await rm.kill_switch.persist()

        # 模拟进程重启: 新实例从 DB 恢复急停状态
        rm2 = RiskManager()
        await rm2.kill_switch.load_from_db()
        assert rm2.kill_switch.is_armed is True
        assert "权益对账漂移" in rm2.kill_switch.reason

        # 冻结态关闭所有方向闸门
        assert not rm2.can_trade()
        assert not rm2.can_buy()
        assert not rm2.can_sell()
