"""V10.7(P0-d): 交易所真相对账(订单/成交维度)测试

验证:
- 本地 filled_quantity 与交易所 myTrades 成交额合计一致 -> 无差异;
- 不一致 -> fill_truth_mismatch; 交易所无该订单成交 -> fill_truth_missing;
- 交易所 myTrades 有本地无记录的成交 -> orphan_trade;
- 窗口过滤: 超过 window_seconds 的旧订单不检查。
"""

from datetime import datetime, timedelta, timezone

from at01_common.database import AsyncSessionLocal
from at01_common.models import Order
from at50_execution.exchange_truth_reconciler import ExchangeTruthReconciler

SYMBOL = "SOLUSDT"


class _FakeRest:
    def __init__(self, trades=None):
        self.trades = trades or []

    async def get_my_trades(self, symbol, limit=None, order_id=None):
        return self.trades


def _trade(order_id, qty):
    return {"orderId": order_id, "qty": qty, "price": "100.0"}


async def _insert_order(cid, *, filled=0.0, eid=None, created_at=None):
    async with AsyncSessionLocal() as session:
        session.add(Order(
            client_order_id=cid, symbol=SYMBOL, side="BUY",
            order_type="MARKET", quantity=1.0, status="FILLED",
            is_paper=False, filled_quantity=filled,
            exchange_order_id=eid, created_at=created_at,
        ))
        await session.commit()


class TestExchangeTruth:
    async def test_consistent_no_diff(self, db_tables):
        await _insert_order("cid-1", filled=1.0, eid="123")
        rec = ExchangeTruthReconciler(_FakeRest(trades=[_trade("123", "1.0")]))
        assert await rec.reconcile(SYMBOL) == []

    async def test_fill_truth_mismatch(self, db_tables):
        await _insert_order("cid-2", filled=1.0, eid="123")
        rec = ExchangeTruthReconciler(_FakeRest(trades=[_trade("123", "0.5")]))
        diffs = await rec.reconcile(SYMBOL)
        assert len(diffs) == 1
        assert diffs[0]["type"] == "fill_truth_mismatch"
        assert diffs[0]["expected"] == 1.0
        assert diffs[0]["actual"] == 0.5

    async def test_fill_truth_missing(self, db_tables):
        await _insert_order("cid-3", filled=1.0, eid="123")
        rec = ExchangeTruthReconciler(_FakeRest(trades=[]))  # 交易所无该订单成交
        diffs = await rec.reconcile(SYMBOL)
        assert len(diffs) == 1
        assert diffs[0]["type"] == "fill_truth_missing"
        assert diffs[0]["client_order_id"] == "cid-3"

    async def test_orphan_trade(self, db_tables):
        # 本地无任何订单, 交易所有一笔成交 -> 孤儿成交
        rec = ExchangeTruthReconciler(_FakeRest(trades=[_trade("999", "1.0")]))
        diffs = await rec.reconcile(SYMBOL)
        assert len(diffs) == 1
        assert diffs[0]["type"] == "orphan_trade"
        assert diffs[0]["exchange_order_id"] == "999"

    async def test_window_filter_skips_old_order(self, db_tables):
        # 2 小时前的旧订单(超出 window_seconds=900)不检查成交维度
        old = datetime.now(timezone.utc) - timedelta(hours=2)
        await _insert_order("cid-4", filled=1.0, eid="123", created_at=old)
        rec = ExchangeTruthReconciler(_FakeRest(trades=[]))  # 交易所无成交
        assert await rec.reconcile(SYMBOL) == []  # 旧订单被窗口过滤, 不报 fill_truth_missing

    async def test_multiple_trades_summed(self, db_tables):
        await _insert_order("cid-5", filled=1.5, eid="123")
        rec = ExchangeTruthReconciler(
            _FakeRest(trades=[_trade("123", "0.7"), _trade("123", "0.8")])
        )
        assert await rec.reconcile(SYMBOL) == []  # 0.7 + 0.8 == 1.5
