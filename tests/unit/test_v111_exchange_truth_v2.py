"""V11.1(P0-1): Exchange Truth V2 — myTrades 分页完整性 + 降级语义测试

验证:
- 分页耗尽/数据不完整时, 只返回完整性信号(truth_incomplete / pagination_exhausted),
  不误判 fill_truth_missing / fill_truth_mismatch / orphan_trade;
- 重复成交 id 去重后成交额合计仍正确(不重复计数), 仅报 trade_duplicate;
- 跳号(trade_id_gap)不影响成交核对(仅可观测性);
- 真相对账窗口从本地订单创建时间推导(去硬编码 15min)。
"""

from datetime import datetime, timedelta, timezone

import pytest

from at01_common.database import AsyncSessionLocal
from at01_common.models import Order
from at10_market.market_rest_client import MyTradesResult
from at60_execution.exchange_truth_reconciler import ExchangeTruthReconciler

SYMBOL = "SOLUSDT"


class _FakeRest:
    """返回 MyTradesResult 的 rest(带 get_my_trades_all), 记录请求 start_time"""
    def __init__(self, result=None, trades=None):
        self.result = result
        self.trades = trades or []
        self.start_time = None

    async def get_my_trades_all(self, symbol, start_time=None, end_time=None,
                                limit=1000, max_pages=10):
        self.start_time = start_time
        if self.result is not None:
            return self.result
        return MyTradesResult(trades=self.trades, complete=True, pagination_exhausted=False,
                              page_count=1, duplicate_ids=[], gaps=[])


def _trade(order_id, qty, tid=1):
    return {"orderId": order_id, "qty": qty, "price": "100.0", "id": tid}


async def _insert_order(cid, *, filled=0.0, eid=None, created_at=None):
    async with AsyncSessionLocal() as session:
        session.add(Order(
            client_order_id=cid, symbol=SYMBOL, side="BUY",
            order_type="MARKET", quantity=1.0, status="FILLED",
            is_paper=False, filled_quantity=filled,
            exchange_order_id=eid, created_at=created_at,
        ))
        await session.commit()


class TestExchangeTruthV2:
    async def test_truth_incomplete_skips_fill_check(self, db_tables):
        # 本地有已成交订单, 但交易所成交历史分页耗尽(不完整) -> 只报完整性信号, 不误判 missing
        await _insert_order("cid-1", filled=1.0, eid="123")
        incomplete = MyTradesResult(
            trades=[], complete=False, pagination_exhausted=True, page_count=10,
            duplicate_ids=[], gaps=[],
        )
        rec = ExchangeTruthReconciler(_FakeRest(result=incomplete))
        diffs = await rec.reconcile(SYMBOL)
        types = {d["type"] for d in diffs}
        assert "truth_incomplete" in types
        assert "pagination_exhausted" in types
        # 关键: 不完整时绝不误报资金级差异
        assert "fill_truth_missing" not in types
        assert "fill_truth_mismatch" not in types
        assert "orphan_trade" not in types

    async def test_incomplete_suppresses_orphan_false_positive(self, db_tables):
        # 不完整数据里含未知 orderId, 也不能据此判 orphan(可能只是被截断漏掉了本地订单)
        unknown = MyTradesResult(
            trades=[_trade("999", "1.0", tid=1)], complete=False,
            pagination_exhausted=True, page_count=10, duplicate_ids=[], gaps=[],
        )
        rec = ExchangeTruthReconciler(_FakeRest(result=unknown))
        diffs = await rec.reconcile(SYMBOL)
        assert not any(d["type"] == "orphan_trade" for d in diffs)

    async def test_duplicate_dedup_keeps_fill_consistent(self, db_tables):
        # get_my_trades_all 已去重(trades 只保留一次), duplicate_ids 记录重复 -> 成交额合计正确, 且报 trade_duplicate
        await _insert_order("cid-2", filled=1.0, eid="123")
        dup = MyTradesResult(
            trades=[_trade("123", "1.0", tid=7)],  # 去重后仅一条
            complete=True, pagination_exhausted=False, page_count=1, duplicate_ids=[7], gaps=[],
        )
        rec = ExchangeTruthReconciler(_FakeRest(result=dup))
        diffs = await rec.reconcile(SYMBOL)
        types = {d["type"] for d in diffs}
        assert "trade_duplicate" in types
        assert "fill_truth_mismatch" not in types  # 去重后 1.0 == 1.0, 无漂移
        assert "fill_truth_missing" not in types

    async def test_trade_id_gap_reported_without_fill_impact(self, db_tables):
        # 两笔不同订单的成交 id 跳号(1 -> 5, myTrades 稀疏正常) -> 报 trade_id_gap, 但成交核对各自一致
        await _insert_order("cid-3a", filled=1.0, eid="123")
        await _insert_order("cid-3b", filled=1.0, eid="456")
        gapped = MyTradesResult(
            trades=[_trade("123", "1.0", tid=1), _trade("456", "1.0", tid=5)],
            complete=True, pagination_exhausted=False, page_count=1,
            duplicate_ids=[], gaps=[{"from": 1, "to": 5, "missing": 3}],
        )
        rec = ExchangeTruthReconciler(_FakeRest(result=gapped))
        diffs = await rec.reconcile(SYMBOL)
        types = {d["type"] for d in diffs}
        assert "trade_id_gap" in types
        assert "fill_truth_mismatch" not in types

    async def test_window_derived_from_order_created_at(self, db_tables):
        # 真相对账窗口应从本地订单 created_at 推导(减 60s buffer), 而非固定 now-900s
        created = datetime.now(timezone.utc) - timedelta(seconds=300)
        await _insert_order("cid-4", filled=1.0, eid="123", created_at=created)
        fake = _FakeRest(trades=[_trade("123", "1.0", tid=1)])
        rec = ExchangeTruthReconciler(fake)
        await rec.reconcile(SYMBOL)
        expected_ms = int((created - timedelta(seconds=60)).timestamp() * 1000)
        assert fake.start_time == pytest.approx(expected_ms, abs=5000)  # 5s 容差

    async def test_complete_consistent_no_diff(self, db_tables):
        await _insert_order("cid-5", filled=1.0, eid="123")
        rec = ExchangeTruthReconciler(_FakeRest(trades=[_trade("123", "1.0", tid=1)]))
        assert await rec.reconcile(SYMBOL) == []
