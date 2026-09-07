"""V10.3: Lot 会计(PositionLot + SellAllocation + FIFO)测试

- FIFO 精确已实现盈亏 + 剩余成本(附加审计层, 不动平均成本口径)
- 费用处理(买入费摊入 lot 成本, 卖出费一次性扣)
- 多 lot 部分卖出 -> SellAllocation 逐笔分配
- 全平仓收敛: FIFO 累计 realized == 平均成本累计 realized
- 对账不变量: 开仓 lot 总和 == 持仓量
- AccountLedger 落 realized_pnl/matched_cost
- 持久化回读(崩溃恢复 FIFO 队列)
"""

import pytest
from sqlalchemy import select

from at60_risk.risk_lot import LotTracker


# ---------- FIFO 核心 ----------

class TestFifoCore:
    async def test_fifo_realized_and_remaining(self, db_tables):
        lt = LotTracker()
        await lt.add_buy("SOLUSDT", 1.0, 100.0)
        await lt.add_buy("SOLUSDT", 1.0, 200.0)
        realized, matched_cost, allocs = await lt.allocate_sell("SOLUSDT", 1.0, 150.0)
        assert realized == pytest.approx(50.0)
        assert matched_cost == pytest.approx(100.0)
        assert len(allocs) == 1
        assert allocs[0]["lot_price"] == pytest.approx(100.0)
        assert allocs[0]["quantity"] == pytest.approx(1.0)
        assert lt.open_quantity("SOLUSDT") == pytest.approx(1.0)
        assert lt.remaining_cost_basis("SOLUSDT") == pytest.approx(200.0)

    async def test_partial_sell_across_multiple_lots(self, db_tables):
        lt = LotTracker()
        await lt.add_buy("SOLUSDT", 1.0, 100.0)
        await lt.add_buy("SOLUSDT", 1.0, 200.0)
        realized, matched_cost, allocs = await lt.allocate_sell("SOLUSDT", 1.5, 150.0)
        # 先消费 lot1 全部(1.0), 再消费 lot2 一半(0.5)
        assert len(allocs) == 2
        assert allocs[0]["quantity"] == pytest.approx(1.0)
        assert allocs[0]["lot_price"] == pytest.approx(100.0)
        assert allocs[1]["quantity"] == pytest.approx(0.5)
        assert allocs[1]["lot_price"] == pytest.approx(200.0)
        gross = (150 - 100) * 1.0 + (150 - 200) * 0.5  # 50 - 25 = 25
        assert realized == pytest.approx(gross)
        assert matched_cost == pytest.approx(100.0 * 1.0 + 200.0 * 0.5)
        assert lt.open_quantity("SOLUSDT") == pytest.approx(0.5)
        assert lt.remaining_cost_basis("SOLUSDT") == pytest.approx(100.0)

    async def test_oversell_truncated(self, db_tables):
        lt = LotTracker()
        await lt.add_buy("SOLUSDT", 1.0, 100.0)
        realized, _, _ = await lt.allocate_sell("SOLUSDT", 5.0, 150.0)
        assert realized == pytest.approx(50.0)
        assert lt.open_quantity("SOLUSDT") == pytest.approx(0.0)


# ---------- 费用 ----------

class TestFees:
    async def test_buy_fee_amortized_into_lot_cost(self, db_tables):
        lt = LotTracker()
        await lt.add_buy("SOLUSDT", 1.0, 100.0, fee_quote=1.0)
        realized, matched_cost, _ = await lt.allocate_sell("SOLUSDT", 1.0, 150.0, fee_quote=1.0)
        # 单位成本 = (100 + 1)/1 = 101
        assert matched_cost == pytest.approx(101.0)
        assert realized == pytest.approx((150 - 101) * 1.0 - 1.0)

    async def test_sell_fee_expensed_once(self, db_tables):
        lt = LotTracker()
        await lt.add_buy("SOLUSDT", 1.0, 100.0)
        await lt.add_buy("SOLUSDT", 1.0, 200.0)
        realized, _, _ = await lt.allocate_sell("SOLUSDT", 2.0, 150.0, fee_quote=3.0)
        gross = (150 - 100) * 1.0 + (150 - 200) * 1.0  # 50 - 50 = 0
        assert realized == pytest.approx(gross - 3.0)


# ---------- 收敛 ----------

class TestConvergence:
    async def test_full_flatten_matches_avg_cost(self, db_tables):
        from at60_risk.risk_position import PositionManager

        pm = PositionManager()
        lt = LotTracker()
        pm.apply_buy("SOLUSDT", 1.0, 100.0)
        pm.apply_buy("SOLUSDT", 1.0, 200.0)
        await lt.add_buy("SOLUSDT", 1.0, 100.0)
        await lt.add_buy("SOLUSDT", 1.0, 200.0)
        _, avg1 = pm.apply_sell("SOLUSDT", 1.0, 150.0)
        _, avg2 = pm.apply_sell("SOLUSDT", 1.0, 150.0)
        fifo1, _, _ = await lt.allocate_sell("SOLUSDT", 1.0, 150.0)
        fifo2, _, _ = await lt.allocate_sell("SOLUSDT", 1.0, 150.0)
        assert (avg1 + avg2) == pytest.approx(fifo1 + fifo2)


# ---------- 对账不变量 ----------

class TestReconcile:
    async def test_reconcile_match(self, db_tables):
        lt = LotTracker()
        await lt.add_buy("SOLUSDT", 2.0, 100.0)
        assert lt.reconcile("SOLUSDT", 2.0) is None

    async def test_reconcile_mismatch(self, db_tables):
        lt = LotTracker()
        await lt.add_buy("SOLUSDT", 1.0, 100.0)
        d = lt.reconcile("SOLUSDT", 2.0)
        assert d is not None
        assert d["diff"] == pytest.approx(1.0)
        assert d["position_quantity"] == pytest.approx(2.0)
        assert d["lot_quantity"] == pytest.approx(1.0)


# ---------- AccountLedger FIFO 列 ----------

class TestAccountLedger:
    async def test_record_sell_writes_fifo_columns(self, db_tables):
        from at01_common.database import AsyncSessionLocal
        from at01_common.models import AccountLedger
        from at60_risk.risk_account_ledger import AccountLedgerWriter

        w = AccountLedgerWriter()
        ok = await w.record(
            ts=1000, symbol="SOLUSDT", bucket="trade", side="SELL",
            cash_before=1000.0, cash_after=1050.0,
            pos_before=1.0, pos_after=0.0,
            reason="test", related_order_id="cid-s1",
            realized_pnl=50.0, matched_cost=100.0,
        )
        assert ok
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(
                select(AccountLedger).where(AccountLedger.related_order_id == "cid-s1")
            )).scalars().all()
        assert len(rows) == 2
        for r in rows:
            assert r.realized_pnl == pytest.approx(50.0)
            assert r.matched_cost == pytest.approx(100.0)

    async def test_record_buy_zero_fifo(self, db_tables):
        from at01_common.database import AsyncSessionLocal
        from at01_common.models import AccountLedger
        from at60_risk.risk_account_ledger import AccountLedgerWriter

        w = AccountLedgerWriter()
        await w.record(
            ts=1000, symbol="SOLUSDT", bucket="trade", side="BUY",
            cash_before=1000.0, cash_after=900.0,
            pos_before=0.0, pos_after=1.0,
            related_order_id="cid-b1",
        )
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(
                select(AccountLedger).where(AccountLedger.related_order_id == "cid-b1")
            )).scalars().all()
        assert len(rows) == 2
        for r in rows:
            assert r.realized_pnl == pytest.approx(0.0)
            assert r.matched_cost == pytest.approx(0.0)


# ---------- 持久化 ----------

class TestPersistence:
    async def test_load_from_db_restores_lots(self, db_tables):
        lt = LotTracker()
        await lt.add_buy("SOLUSDT", 1.0, 100.0, client_order_id="cid-b1")
        await lt.add_buy("SOLUSDT", 1.0, 200.0, client_order_id="cid-b2")
        # 新实例从 DB 恢复(崩溃恢复)
        t2 = LotTracker()
        await t2.load_from_db()
        assert t2.open_quantity("SOLUSDT") == pytest.approx(2.0)
        assert t2.remaining_cost_basis("SOLUSDT") == pytest.approx(300.0)
        # FIFO 顺序保持(先买先卖)
        _, _, allocs = await t2.allocate_sell("SOLUSDT", 1.0, 150.0)
        assert allocs[0]["lot_price"] == pytest.approx(100.0)

    async def test_sell_allocation_persisted(self, db_tables):
        from at01_common.database import AsyncSessionLocal
        from at01_common.models import PositionLot, SellAllocation

        lt = LotTracker()
        await lt.add_buy("SOLUSDT", 1.0, 100.0, client_order_id="cid-b1")
        await lt.add_buy("SOLUSDT", 1.0, 200.0, client_order_id="cid-b2")
        await lt.allocate_sell("SOLUSDT", 1.5, 150.0, client_order_id="cid-s1")
        async with AsyncSessionLocal() as session:
            allocs = (await session.execute(
                select(SellAllocation).where(SellAllocation.symbol == "SOLUSDT")
            )).scalars().all()
            lots = (await session.execute(
                select(PositionLot).where(PositionLot.symbol == "SOLUSDT")
            )).scalars().all()
        assert len(allocs) == 2
        by_id = {l.id: l for l in lots}
        assert by_id[allocs[0].lot_id].status == "closed"
        assert by_id[allocs[1].lot_id].status == "open"
        assert by_id[allocs[1].lot_id].quantity == pytest.approx(0.5)
        assert sum(a.quantity for a in allocs) == pytest.approx(1.5)

    async def test_closed_lot_quantity_zeroed(self, db_tables):
        # 全平仓 lot 落库时应 status=closed 且 quantity=0(回归: 历史 bug 只置 closed 不归零,
        # 导致交叉对账按残留 quantity 误报 buy_lot_mismatch)
        from at01_common.database import AsyncSessionLocal
        from at01_common.models import PositionLot

        lt = LotTracker()
        await lt.add_buy("SOLUSDT", 1.0, 100.0, client_order_id="cid-b1")
        await lt.allocate_sell("SOLUSDT", 1.0, 150.0, client_order_id="cid-s1")
        async with AsyncSessionLocal() as session:
            lots = (await session.execute(
                select(PositionLot).where(PositionLot.symbol == "SOLUSDT")
            )).scalars().all()
        assert len(lots) == 1
        assert lots[0].status == "closed"
        assert lots[0].quantity == pytest.approx(0.0)
