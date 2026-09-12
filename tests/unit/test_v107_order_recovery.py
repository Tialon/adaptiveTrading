"""V10.7(P0-c): 订单恢复引擎(Order Recovery Engine)测试

验证:
- UNKNOWN/SUBMITTING 订单按交易所真相收敛(FILLED -> 完整记账 / CANCELED / 回填 eid);
- 订单不存在(-2013 或空)-> 撤销;
- RECOVERY_REQUIRED BUY 账务重建(进程内: 只补 DB 镜像; 重启后: 完整记账);
- RECOVERY_REQUIRED SELL 账务重建(从 DB 开仓 lot 确定性重放, 不再冻结; 详见 test_v114);
- 幂等: 恢复后 accounting_state=RECOVERED, 二次调用 skip, 不重复记账。
"""

from sqlalchemy import func, select

from at01_common.database import AsyncSessionLocal
from at01_common.models import Order, Position, PositionLot
from at60_execution.execution_executor import ExecutionEngine
from at60_execution.order_recovery import OrderRecoveryEngine
from at50_risk.risk_manager import RiskManager

SYMBOL = "SOLUSDT"

FILLED = {
    "orderId": 123, "status": "FILLED",
    "executedQty": "1.0", "cummulativeQuoteQty": "100.0", "price": "100.0",
}
CANCELED = {"orderId": 123, "status": "CANCELED", "executedQty": "0.0"}
OPEN = {"orderId": 123, "status": "NEW", "executedQty": "0.0"}


class _FakeRest:
    """最小 REST 桩: 仅覆盖订单恢复需要的 get_order / get_my_trades"""

    def __init__(self, orders=None, trades=None):
        self.orders = orders or {}
        self.trades = trades or {}

    async def get_order(self, symbol, exchange_order_id=None, orig_client_order_id=None):
        if orig_client_order_id is not None:
            return self.orders.get(orig_client_order_id)
        return self.orders.get(str(exchange_order_id))

    async def get_my_trades(self, symbol, order_id=None, limit=None):
        return self.trades.get(str(order_id), [])


async def _count(model) -> int:
    async with AsyncSessionLocal() as session:
        return (await session.execute(select(func.count()).select_from(model))).scalar()


async def _order(cid: str) -> Order:
    async with AsyncSessionLocal() as session:
        return (
            await session.execute(
                select(Order).where(Order.client_order_id == cid)
            )
        ).scalar_one()


async def _insert_order(cid, *, side="BUY", status="UNKNOWN", accounting_state="OK",
                        filled=0.0, avg=None, eid=None):
    async with AsyncSessionLocal() as session:
        session.add(Order(
            client_order_id=cid, symbol=SYMBOL, side=side,
            order_type="MARKET", quantity=1.0, status=status,
            is_paper=False, accounting_state=accounting_state,
            filled_quantity=filled, avg_fill_price=avg, exchange_order_id=eid,
        ))
        await session.commit()


def _engine():
    e = ExecutionEngine(risk_manager=RiskManager())
    e.is_paper = False
    e.rest = _FakeRest()
    return e


class TestRecoverStatus:
    async def test_unknown_filled_converges(self, db_tables):
        e = _engine()
        e.rest = _FakeRest(orders={"cid-1": FILLED})
        await _insert_order("cid-1", side="BUY", status="UNKNOWN")

        recovery = OrderRecoveryEngine(e.rest, e, e.risk)
        assert await recovery.recover(SYMBOL) == []

        o = await _order("cid-1")
        assert o.status == "FILLED"
        assert o.accounting_state == "RECOVERED"
        assert o.exchange_order_id == "123"
        assert await _count(Position) == 1
        assert await _count(PositionLot) == 1
        assert e.risk.positions.get(SYMBOL).quantity == 1.0

        # 幂等: 二次扫描不再收敛(已离开待恢复集合)
        assert await recovery.recover(SYMBOL) == []

    async def test_unknown_canceled_converges(self, db_tables):
        e = _engine()
        e.rest = _FakeRest(orders={"cid-2": CANCELED})
        await _insert_order("cid-2", side="BUY", status="UNKNOWN")

        recovery = OrderRecoveryEngine(e.rest, e, e.risk)
        assert await recovery.recover(SYMBOL) == []

        o = await _order("cid-2")
        assert o.status == "CANCELED"
        assert await _count(Position) == 0  # 未记账
        assert await _count(PositionLot) == 0

    async def test_unknown_not_found_marks_canceled(self, db_tables):
        e = _engine()
        e.rest = _FakeRest()  # 查不到
        await _insert_order("cid-3", side="BUY", status="UNKNOWN")

        recovery = OrderRecoveryEngine(e.rest, e, e.risk)
        assert await recovery.recover(SYMBOL) == []

        o = await _order("cid-3")
        assert o.status == "CANCELED"

    async def test_canceled_partial_fill_accounts(self, db_tables):
        """V11.0(F3): 终态前已部分成交(executedQty>0)须记账, 不静默丢弃"""
        e = _engine()
        e.rest = _FakeRest(orders={"cid-partial": {
            "orderId": 777, "status": "CANCELED",
            "executedQty": "0.5", "cummulativeQuoteQty": "50.0", "price": "100.0",
        }})
        await _insert_order("cid-partial", side="BUY", status="UNKNOWN")

        recovery = OrderRecoveryEngine(e.rest, e, e.risk)
        assert await recovery.recover(SYMBOL) == []

        o = await _order("cid-partial")
        assert o.status == "CANCELED"  # 终态落 CANCELED, 但成交已记账
        assert o.accounting_state == "RECOVERED"
        assert o.filled_quantity == 0.5
        assert e.risk.positions.get(SYMBOL).quantity == 0.5
        assert await _count(PositionLot) == 1

    async def test_unknown_still_open_backfills_eid(self, db_tables):
        e = _engine()
        e.rest = _FakeRest(orders={"cid-4": OPEN})
        await _insert_order("cid-4", side="BUY", status="UNKNOWN")

        recovery = OrderRecoveryEngine(e.rest, e, e.risk)
        assert await recovery.recover(SYMBOL) == []

        o = await _order("cid-4")
        assert o.status == "NEW"  # 仍挂单, 回填状态
        assert o.exchange_order_id == "123"
        assert o.accounting_state == "OK"  # 未成交, 不记账


class TestRecoverAccounting:
    async def test_recovery_required_buy_rebuilds_mirror(self, db_tables):
        """进程内失败: 内存已含 lot(权威态), 只补 DB 镜像, 不重复变更内存"""
        e = _engine()
        cid = "cid-rebuild"
        await _insert_order(cid, side="BUY", status="FILLED",
                            accounting_state="RECOVERY_REQUIRED", filled=1.0, avg=100.0, eid="123")
        # 模拟记账失败后的内存权威态(持仓已 bump, lot 已含 id=None), DB 已回滚
        e.risk.positions.apply_buy(SYMBOL, 1.0, 100.0, 0.0)
        e.lot_tracker.lots.setdefault(SYMBOL, []).append({
            "id": None, "symbol": SYMBOL, "quantity": 1.0, "price": 100.0,
            "fee_quote": 0.0, "client_order_id": cid, "exchange_order_id": "123",
        })
        assert await _count(Position) == 0
        assert await _count(PositionLot) == 0

        recovery = OrderRecoveryEngine(e.rest, e, e.risk)
        assert await recovery.recover(SYMBOL) == []

        assert await _count(Position) == 1
        assert await _count(PositionLot) == 1
        lot = e.lot_tracker.lots[SYMBOL][0]
        assert lot["id"] is not None  # id 已回填
        o = await _order(cid)
        assert o.accounting_state == "RECOVERED"
        # 内存持仓未重复变更
        assert e.risk.positions.get(SYMBOL).quantity == 1.0

    async def test_recovery_required_buy_post_restart_full_accounting(self, db_tables):
        """重启后: 内存无 lot, 走完整记账(内存 + DB)"""
        e = _engine()
        e.rest = _FakeRest(orders={"cid-full": FILLED})
        cid = "cid-full"
        await _insert_order(cid, side="BUY", status="FILLED",
                            accounting_state="RECOVERY_REQUIRED", filled=1.0, avg=100.0, eid="123")
        assert e._find_lot(SYMBOL, cid) is None  # 重启后无内存 lot

        recovery = OrderRecoveryEngine(e.rest, e, e.risk)
        assert await recovery.recover(SYMBOL) == []

        assert await _count(Position) == 1
        assert await _count(PositionLot) == 1
        o = await _order(cid)
        assert o.accounting_state == "RECOVERED"
        assert e.risk.positions.get(SYMBOL).quantity == 1.0

    async def test_recovery_required_sell_recovers(self, db_tables):
        """V11.1(P0-4): SELL 不再冻结 —— 从 DB 开仓 lot 确定性重建(FIFO 细节见 test_v114)"""
        e = _engine()
        cid = "cid-sell"
        await _insert_order(cid, side="SELL", status="FILLED",
                            accounting_state="RECOVERY_REQUIRED", filled=1.0, avg=110.0, eid="456")

        recovery = OrderRecoveryEngine(e.rest, e, e.risk)
        assert await recovery.recover(SYMBOL) == []  # 不再未解决

        o = await _order(cid)
        assert o.accounting_state == "RECOVERED"  # 已自愈, 不再冻结


class TestApplyRecoveredFill:
    async def test_idempotent_skip(self, db_tables):
        e = _engine()
        cid = "cid-idem"
        await _insert_order(cid, side="BUY", status="UNKNOWN")

        r1 = await e.apply_recovered_fill(
            symbol=SYMBOL, side="BUY", client_order_id=cid,
            exchange_order_id="123", fill_qty=1.0, fill_price=100.0, fee=0.0,
        )
        assert r1 == "filled"
        r2 = await e.apply_recovered_fill(
            symbol=SYMBOL, side="BUY", client_order_id=cid,
            exchange_order_id="123", fill_qty=1.0, fill_price=100.0, fee=0.0,
        )
        assert r2 == "skip"
        assert e.risk.positions.get(SYMBOL).quantity == 1.0  # 只涨一次
        assert await _count(PositionLot) == 1

    async def test_normal_terminal_skip(self, db_tables):
        e = _engine()
        cid = "cid-done"
        await _insert_order(cid, side="BUY", status="FILLED",
                            accounting_state="OK", filled=1.0, avg=100.0)
        r = await e.apply_recovered_fill(
            symbol=SYMBOL, side="BUY", client_order_id=cid,
            exchange_order_id="123", fill_qty=1.0, fill_price=100.0, fee=0.0,
        )
        assert r == "skip"
        assert await _count(PositionLot) == 0  # 未重复记账

    async def test_recovered_fill_atomic_status_and_accounting(self, db_tables):
        """V11.0(F4): 记账/状态/accounting_state 同事务原子落, 二次调用 skip 不重复"""
        e = _engine()
        cid = "cid-atomic"
        await _insert_order(cid, side="BUY", status="UNKNOWN")

        r1 = await e.apply_recovered_fill(
            symbol=SYMBOL, side="BUY", client_order_id=cid,
            exchange_order_id="123", fill_qty=1.0, fill_price=100.0, fee=0.0,
        )
        assert r1 == "filled"
        o = await _order(cid)
        assert o.status == "FILLED"
        assert o.accounting_state == "RECOVERED"
        assert o.filled_quantity == 1.0
        assert o.avg_fill_price == 100.0
        assert await _count(PositionLot) == 1

        r2 = await e.apply_recovered_fill(
            symbol=SYMBOL, side="BUY", client_order_id=cid,
            exchange_order_id="123", fill_qty=1.0, fill_price=100.0, fee=0.0,
        )
        assert r2 == "skip"
        assert await _count(PositionLot) == 1  # 不重复记账

    async def test_recovered_fill_uses_trade_fee(self, db_tables):
        """V11.0(F11): 恢复路径从 myTrades 重摄取真实手续费, 摊入 lot 成本"""
        e = _engine()
        e.rest = _FakeRest(trades={"123": [{
            "id": 1, "orderId": "123", "price": "100.0", "qty": "1.0",
            "quoteQty": "100.0", "commission": "0.5", "commissionAsset": "USDT",
            "time": 1700000000000,
        }]})
        cid = "cid-fee"
        await _insert_order(cid, side="BUY", status="UNKNOWN")

        r = await e.apply_recovered_fill(
            symbol=SYMBOL, side="BUY", client_order_id=cid,
            exchange_order_id="123", fill_qty=1.0, fill_price=100.0, fee=0.0,
        )
        assert r == "filled"
        async with AsyncSessionLocal() as session:
            lot = (await session.execute(
                select(PositionLot).where(PositionLot.client_order_id == cid)
            )).scalar_one()
        assert lot.fee_quote == 0.5  # 真实手续费流入(而非默认 0)
        assert lot.price == 100.5  # 单位成本含摊入买入费
