"""V10.4: 三维交叉对账(Order / Fill / Ledger / Lot 一致性)测试

逐笔核对同一 client_order_id 在 Order / OrderFill / AccountLedger /
PositionLot+SellAllocation 四个维度是否自洽, 任一维度漂移 -> 对应差异类型。
"""

from datetime import datetime, timedelta, timezone

from at01_common.database import AsyncSessionLocal
from at01_common.models import AccountLedger, Order, OrderFill, PositionLot, SellAllocation
from at50_execution.cross_reconciler import CrossReconciler


async def _insert_order(cid, side="BUY", filled=1.0, status="FILLED", created_at=None,
                        is_paper=False):
    async with AsyncSessionLocal() as session:
        o = Order(
            client_order_id=cid, symbol="SOLUSDT", side=side, quantity=1.0,
            price=100.0, status=status, filled_quantity=filled, is_paper=is_paper,
        )
        if created_at is not None:
            o.created_at = created_at
        session.add(o)
        await session.commit()


async def _insert_fill(cid, side="BUY", qty=1.0, trade_id=1):
    async with AsyncSessionLocal() as session:
        session.add(OrderFill(
            client_order_id=cid, symbol="SOLUSDT", side=side, quantity=qty,
            exchange_order_id="100", exchange_trade_id=trade_id,
            fill_idempotency_key=f"100:{trade_id}",
            price=100.0, quote_quantity=qty * 100.0,
        ))
        await session.commit()


async def _insert_ledger(cid, asset="SOL", change=1.0, side="BUY"):
    async with AsyncSessionLocal() as session:
        session.add(AccountLedger(
            ts=1000, symbol="SOLUSDT", bucket="trade", side=side, asset=asset,
            change_amount=change, related_order_id=cid,
        ))
        await session.commit()


async def _insert_lot(cid, qty=1.0) -> int:
    async with AsyncSessionLocal() as session:
        lot = PositionLot(
            symbol="SOLUSDT", quantity=qty, price=100.0,
            client_order_id=cid, status="open",
        )
        session.add(lot)
        await session.commit()
        return lot.id


async def _insert_alloc(sell_cid, qty=1.0, lot_id=None):
    async with AsyncSessionLocal() as session:
        session.add(SellAllocation(
            symbol="SOLUSDT", sell_client_order_id=sell_cid, quantity=qty,
            lot_id=lot_id, lot_price=100.0, sell_price=150.0, realized_pnl=0.0,
        ))
        await session.commit()


async def _reconcile():
    return await CrossReconciler().reconcile("SOLUSDT")


def _types(diffs):
    return {d["type"] for d in diffs}


class TestConsistent:
    async def test_buy_consistent_ok(self, db_tables):
        # 实盘: 不落 AccountLedger, 无账本维度也应自洽(回归: 不得误报 ledger_missing)
        await _insert_order("c1", side="BUY", filled=1.0)
        await _insert_fill("c1", side="BUY", qty=1.0)
        await _insert_lot("c1", qty=1.0)
        assert await _reconcile() == []

    async def test_sell_consistent_ok(self, db_tables):
        await _insert_order("c1", side="SELL", filled=1.0)
        await _insert_fill("c1", side="SELL", qty=1.0)
        await _insert_alloc("c1", qty=1.0)
        assert await _reconcile() == []

    async def test_buy_consistent_paper_with_ledger(self, db_tables):
        # 纸面: 落 AccountLedger, 四维全自洽
        await _insert_order("c1", side="BUY", filled=1.0, is_paper=True)
        await _insert_fill("c1", side="BUY", qty=1.0)
        await _insert_ledger("c1", asset="SOL", change=1.0, side="BUY")
        await _insert_lot("c1", qty=1.0)
        assert await _reconcile() == []

    async def test_sell_consistent_paper_with_ledger(self, db_tables):
        await _insert_order("c1", side="SELL", filled=1.0, is_paper=True)
        await _insert_fill("c1", side="SELL", qty=1.0)
        await _insert_ledger("c1", asset="SOL", change=-1.0, side="SELL")
        await _insert_alloc("c1", qty=1.0)
        assert await _reconcile() == []

    async def test_buy_partial_sold_still_ok(self, db_tables):
        # lot 剩余 0.5 + 已卖出 0.5 == 原始买入 1.0 -> 自洽(纸面 + 账本)
        await _insert_order("c1", side="BUY", filled=1.0, is_paper=True)
        await _insert_fill("c1", side="BUY", qty=1.0)
        await _insert_ledger("c1", asset="SOL", change=1.0, side="BUY")
        lot_id = await _insert_lot("c1", qty=0.5)
        await _insert_alloc("c1", qty=0.5, lot_id=lot_id)
        assert await _reconcile() == []


class TestFillCoverage:
    async def test_fill_missing(self, db_tables):
        await _insert_order("c1", side="BUY", filled=1.0)
        assert "fill_missing" in _types(await _reconcile())

    async def test_fill_mismatch(self, db_tables):
        await _insert_order("c1", side="BUY", filled=1.0)
        await _insert_fill("c1", side="BUY", qty=0.5)
        assert "fill_mismatch" in _types(await _reconcile())

    async def test_fill_side_mismatch(self, db_tables):
        await _insert_order("c1", side="BUY", filled=1.0)
        await _insert_fill("c1", side="SELL", qty=1.0)
        assert "fill_side_mismatch" in _types(await _reconcile())


class TestLedgerPosition:
    async def test_ledger_missing(self, db_tables):
        await _insert_order("c1", side="BUY", filled=1.0, is_paper=True)
        await _insert_fill("c1", side="BUY", qty=1.0)
        assert "ledger_missing" in _types(await _reconcile())

    async def test_ledger_position_mismatch(self, db_tables):
        await _insert_order("c1", side="BUY", filled=1.0, is_paper=True)
        await _insert_fill("c1", side="BUY", qty=1.0)
        await _insert_ledger("c1", asset="SOL", change=2.0, side="BUY")
        assert "ledger_position_mismatch" in _types(await _reconcile())

    async def test_live_order_without_ledger_not_flagged(self, db_tables):
        # 实盘不落账本 -> 不得误报 ledger_missing(核心回归)
        await _insert_order("c1", side="BUY", filled=1.0)
        await _insert_fill("c1", side="BUY", qty=1.0)
        await _insert_lot("c1", qty=1.0)
        diffs = await _reconcile()
        assert "ledger_missing" not in _types(diffs)
        assert diffs == []


class TestLot:
    async def test_buy_lot_mismatch(self, db_tables):
        await _insert_order("c1", side="BUY", filled=1.0)
        await _insert_fill("c1", side="BUY", qty=1.0)
        await _insert_lot("c1", qty=0.5)
        assert "buy_lot_mismatch" in _types(await _reconcile())

    async def test_buy_lot_missing(self, db_tables):
        await _insert_order("c1", side="BUY", filled=1.0)
        await _insert_fill("c1", side="BUY", qty=1.0)
        assert "buy_lot_mismatch" in _types(await _reconcile())


class TestSellAlloc:
    async def test_sell_alloc_mismatch(self, db_tables):
        await _insert_order("c1", side="SELL", filled=1.0)
        await _insert_fill("c1", side="SELL", qty=1.0)
        await _insert_alloc("c1", qty=0.5)
        assert "sell_alloc_mismatch" in _types(await _reconcile())


class TestWindow:
    async def test_old_order_skipped(self, db_tables):
        # 2 小时前的旧订单(若被检查会报 fill_missing), 应被窗口过滤跳过
        await _insert_order(
            "c1", side="BUY", filled=1.0,
            created_at=datetime.now(timezone.utc) - timedelta(hours=2),
        )
        assert await _reconcile() == []
