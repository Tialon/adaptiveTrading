"""V11.1(P0-4): SELL 账务重建(消除 RECOVERY_REQUIRED SELL 人工冻结)测试

验证:
- 从 DB 开仓 lot(权威未消费态)确定性重放 FIFO 分配: SellAllocation + 减 lot +
  Position(平均成本口径)+ Order 同事务;
- 平均成本口径已实现盈亏与 positions.apply_sell 一致(卖出不改 avg_price, 清仓归零);
- 幂等: accounting_state=RECOVERED 后二次调用 skip, 不重复记账;
- 重同步内存: 覆盖进程内失败遗留的「已消费」分歧内存(内存先改、DB 回滚);
- 本地成交数据缺失时从交易所真相补齐; 真实手续费流入; 超卖截断。
"""

import pytest
from sqlalchemy import select

from at01_common.database import AsyncSessionLocal
from at01_common.models import Order, Position, PositionLot, SellAllocation
from at60_execution.execution_executor import ExecutionEngine
from at60_execution.order_recovery import OrderRecoveryEngine
from at50_risk.risk_manager import RiskManager
from at50_risk.risk_position import PositionState

SYMBOL = "SOLUSDT"

FILLED = {
    "orderId": 456, "status": "FILLED",
    "executedQty": "1.0", "cummulativeQuoteQty": "110.0", "price": "110.0",
}


class _FakeRest:
    """最小 REST 桩: 覆盖 SELL 恢复需要的 get_order / get_my_trades"""

    def __init__(self, orders=None, trades=None):
        self.orders = orders or {}
        self.trades = trades or {}

    async def get_order(self, symbol, exchange_order_id=None, orig_client_order_id=None):
        if orig_client_order_id is not None:
            return self.orders.get(orig_client_order_id)
        return self.orders.get(str(exchange_order_id))

    async def get_my_trades(self, symbol, order_id=None, limit=None):
        return self.trades.get(str(order_id), [])


def _engine():
    e = ExecutionEngine(risk_manager=RiskManager())
    e.is_paper = False
    e.rest = _FakeRest()
    return e


async def _insert_order(cid, *, side="SELL", status="FILLED",
                        accounting_state="RECOVERY_REQUIRED", filled=0.0, avg=None, eid=None):
    async with AsyncSessionLocal() as session:
        session.add(Order(
            client_order_id=cid, symbol=SYMBOL, side=side,
            order_type="MARKET", quantity=1.0, status=status,
            is_paper=False, accounting_state=accounting_state,
            filled_quantity=filled, avg_fill_price=avg, exchange_order_id=eid,
        ))
        await session.commit()


async def _seed_db(*, qty, avg, realized=0.0, peak=0.0, lots):
    """落 DB 权威「卖出前」快照: Position + open PositionLot。lots: [(qty, price, cid), ...]"""
    async with AsyncSessionLocal() as session:
        session.add(Position(
            symbol=SYMBOL, quantity=qty, avg_price=avg,
            realized_pnl=realized, peak_price=peak,
        ))
        for (q, p, cid) in lots:
            session.add(PositionLot(
                symbol=SYMBOL, quantity=q, price=p, fee_quote=0.0,
                client_order_id=cid, exchange_order_id=None, status="open",
            ))
        await session.commit()


def _seed_memory(e, *, qty, avg, realized=0.0, peak=0.0, lots=None):
    """落内存持仓 + FIFO lot 队列(可故意设为分歧态以模拟进程内失败)"""
    e.risk.positions.positions[SYMBOL] = PositionState(
        symbol=SYMBOL, quantity=qty, avg_price=avg, realized_pnl=realized, peak_price=peak,
    )
    e.lot_tracker.lots[SYMBOL] = lots if lots is not None else []


async def _position():
    async with AsyncSessionLocal() as session:
        return (await session.execute(
            select(Position).where(Position.symbol == SYMBOL)
        )).scalar_one_or_none()


async def _lots():
    async with AsyncSessionLocal() as session:
        return (await session.execute(
            select(PositionLot).where(PositionLot.symbol == SYMBOL).order_by(PositionLot.id)
        )).scalars().all()


async def _allocs():
    async with AsyncSessionLocal() as session:
        return (await session.execute(select(SellAllocation))).scalars().all()


class TestSellRecovery:
    async def test_rebuilds_fifo_and_position(self, db_tables):
        e = _engine()
        await _seed_db(qty=2.0, avg=100.0, realized=0.0, peak=120.0,
                       lots=[(1.0, 100.0, "b1"), (1.0, 120.0, "b2")])
        _seed_memory(e, qty=2.0, avg=100.0, realized=0.0, peak=120.0,
                     lots=[
                         {"id": None, "symbol": SYMBOL, "quantity": 1.0, "price": 100.0,
                          "fee_quote": 0.0, "client_order_id": "b1", "exchange_order_id": None},
                         {"id": None, "symbol": SYMBOL, "quantity": 1.0, "price": 120.0,
                          "fee_quote": 0.0, "client_order_id": "b2", "exchange_order_id": None},
                     ])
        await _insert_order("cid-s1", side="SELL", filled=1.0, avg=110.0, eid="456")

        recovery = OrderRecoveryEngine(e.rest, e, e.risk)
        assert await recovery.recover(SYMBOL) == []

        pos = await _position()
        assert pos.quantity == pytest.approx(1.0)
        assert pos.avg_price == pytest.approx(100.0)  # 卖出不改 avg_price
        assert pos.realized_pnl == pytest.approx(10.0)  # (110-100)*1

        lots = await _lots()
        assert len(lots) == 2
        by_cid = {l.client_order_id: l for l in lots}
        assert by_cid["b1"].status == "closed"
        assert by_cid["b1"].quantity == pytest.approx(0.0)
        assert by_cid["b2"].status == "open"
        assert by_cid["b2"].quantity == pytest.approx(1.0)

        allocs = await _allocs()
        assert len(allocs) == 1
        assert allocs[0].quantity == pytest.approx(1.0)
        assert allocs[0].lot_id == by_cid["b1"].id  # FIFO 先消费 b1
        assert allocs[0].realized_pnl == pytest.approx(10.0)

        # 内存同步一致
        assert e.risk.positions.get(SYMBOL).quantity == pytest.approx(1.0)
        assert e.risk.positions.get(SYMBOL).realized_pnl == pytest.approx(10.0)
        assert len(e.lot_tracker.lots[SYMBOL]) == 1
        assert e.lot_tracker.lots[SYMBOL][0]["client_order_id"] == "b2"

    async def test_full_close_zeroes_position(self, db_tables):
        e = _engine()
        await _seed_db(qty=1.0, avg=100.0, realized=0.0, peak=120.0, lots=[(1.0, 100.0, "b1")])
        _seed_memory(e, qty=1.0, avg=100.0, peak=120.0)
        await _insert_order("cid-s2", side="SELL", filled=1.0, avg=110.0, eid="456")

        recovery = OrderRecoveryEngine(e.rest, e, e.risk)
        assert await recovery.recover(SYMBOL) == []

        pos = await _position()
        assert pos.quantity == pytest.approx(0.0)
        assert pos.avg_price == pytest.approx(0.0)  # 清仓归零
        assert pos.realized_pnl == pytest.approx(10.0)
        assert pos.peak_price == pytest.approx(0.0)

        lots = await _lots()
        assert lots[0].status == "closed"
        assert lots[0].quantity == pytest.approx(0.0)

        assert e.risk.positions.get(SYMBOL).quantity == 0.0
        assert e.lot_tracker.lots[SYMBOL] == []  # 无开仓 lot

    async def test_idempotent(self, db_tables):
        e = _engine()
        await _seed_db(qty=2.0, avg=100.0, realized=0.0, lots=[(2.0, 100.0, "b1")])
        _seed_memory(e, qty=2.0, avg=100.0)
        await _insert_order("cid-s3", side="SELL", filled=1.0, avg=110.0, eid="456")

        recovery = OrderRecoveryEngine(e.rest, e, e.risk)
        assert await recovery.recover(SYMBOL) == []
        # 二次扫描: 已离开待恢复集合(accounting_state=RECOVERED)
        assert await recovery.recover(SYMBOL) == []

        allocs = await _allocs()
        assert len(allocs) == 1  # 不重复记账
        pos = await _position()
        assert pos.realized_pnl == pytest.approx(10.0)

    async def test_resyncs_divergent_memory(self, db_tables):
        """进程内失败: 内存已被 allocate_sell 消费(分歧态), DB 回滚; 重建后内存收敛"""
        e = _engine()
        await _seed_db(qty=2.0, avg=100.0, realized=0.0, peak=120.0,
                       lots=[(1.0, 100.0, "b1"), (1.0, 120.0, "b2")])
        # 模拟记账失败后: 内存已消费 b1(剩 b2), DB 已回滚(仍含 b1+b2)
        _seed_memory(e, qty=1.0, avg=100.0, realized=10.0, peak=120.0,
                     lots=[{"id": None, "symbol": SYMBOL, "quantity": 1.0, "price": 120.0,
                            "fee_quote": 0.0, "client_order_id": "b2", "exchange_order_id": None}])
        await _insert_order("cid-s4", side="SELL", filled=1.0, avg=110.0, eid="456")

        recovery = OrderRecoveryEngine(e.rest, e, e.risk)
        assert await recovery.recover(SYMBOL) == []

        # DB 正确(权威态 = 卖出前 → 卖出后)
        lots = await _lots()
        by_cid = {l.client_order_id: l for l in lots}
        assert by_cid["b1"].status == "closed"
        assert by_cid["b2"].quantity == pytest.approx(1.0)
        pos = await _position()
        assert pos.quantity == pytest.approx(1.0)
        assert pos.realized_pnl == pytest.approx(10.0)

        # 内存被重同步为正确后卖出态(覆盖分歧, 不二次消费)
        assert e.risk.positions.get(SYMBOL).quantity == pytest.approx(1.0)
        assert e.risk.positions.get(SYMBOL).realized_pnl == pytest.approx(10.0)
        assert len(e.lot_tracker.lots[SYMBOL]) == 1
        assert e.lot_tracker.lots[SYMBOL][0]["client_order_id"] == "b2"
        assert e.lot_tracker.lots[SYMBOL][0]["quantity"] == pytest.approx(1.0)

    async def test_fetches_exchange_fill_when_local_zero(self, db_tables):
        """本地成交数据未回填(事务回滚 filled=0): 从交易所真相补齐"""
        e = _engine()
        e.rest = _FakeRest(orders={"cid-s5": FILLED})
        await _seed_db(qty=1.0, avg=100.0, realized=0.0, lots=[(1.0, 100.0, "b1")])
        _seed_memory(e, qty=1.0, avg=100.0)
        await _insert_order("cid-s5", side="SELL", filled=0.0, avg=None, eid="456")

        recovery = OrderRecoveryEngine(e.rest, e, e.risk)
        assert await recovery.recover(SYMBOL) == []

        async with AsyncSessionLocal() as session:
            o = (await session.execute(
                select(Order).where(Order.client_order_id == "cid-s5")
            )).scalar_one()
        assert o.accounting_state == "RECOVERED"
        assert o.filled_quantity == pytest.approx(1.0)
        assert o.avg_fill_price == pytest.approx(110.0)
        pos = await _position()
        assert pos.realized_pnl == pytest.approx(10.0)

    async def test_real_fee_from_trades(self, db_tables):
        """真实手续费从 myTrades 流入平均成本已实现盈亏"""
        e = _engine()
        e.rest = _FakeRest(trades={"456": [{
            "id": 1, "orderId": "456", "price": "110.0", "qty": "1.0",
            "quoteQty": "110.0", "commission": "0.5", "commissionAsset": "USDT",
            "time": 1700000000000,
        }]})
        await _seed_db(qty=1.0, avg=100.0, realized=0.0, lots=[(1.0, 100.0, "b1")])
        _seed_memory(e, qty=1.0, avg=100.0)
        await _insert_order("cid-s6", side="SELL", filled=1.0, avg=110.0, eid="456")

        recovery = OrderRecoveryEngine(e.rest, e, e.risk)
        assert await recovery.recover(SYMBOL) == []

        pos = await _position()
        assert pos.realized_pnl == pytest.approx(9.5)  # (110-100)*1 - 0.5

    async def test_oversell_truncates(self, db_tables):
        """卖出量超开仓 lot(防御): 按可卖量截断, 不产生负持仓"""
        e = _engine()
        await _seed_db(qty=1.0, avg=100.0, realized=0.0, lots=[(1.0, 100.0, "b1")])
        _seed_memory(e, qty=1.0, avg=100.0)
        await _insert_order("cid-s7", side="SELL", filled=2.0, avg=110.0, eid="456")

        recovery = OrderRecoveryEngine(e.rest, e, e.risk)
        assert await recovery.recover(SYMBOL) == []

        pos = await _position()
        assert pos.quantity == pytest.approx(0.0)  # 不产生负持仓
        assert pos.realized_pnl == pytest.approx(10.0)  # 仅按可卖 1.0 计
        allocs = await _allocs()
        assert len(allocs) == 1
        assert allocs[0].quantity == pytest.approx(1.0)  # 截断为可卖量
