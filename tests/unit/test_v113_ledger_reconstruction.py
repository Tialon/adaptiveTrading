"""V11.1(P0-3): 账本重建引擎(Ledger Reconstruction)测试

验证:
- 纯函数 build_plan: 持仓/lot/卖出分配重建 + FIFO 已实现盈亏 + 现金/账本守恒;
- SAFE_MODE(歧义拒绝): 缺现金锚 / 超卖 / 成交方向不一致 / 成交历史不完整 / 无交易所客户端;
- 不可计价手续费: USDT 现金守恒不受影响、权益置 None、不误判 SAFE_MODE;
- 幂等: 纯函数重复构建结果一致; apply 单事务「先清后插」重复执行终态一致;
- dry-run 不落库 / apply 落库。
"""

import pytest

from at10_market.market_rest_client import MyTradesResult
from at60_execution.ledger_reconstruction import (
    LedgerReconstructionEngine,
    build_plan,
)


def _t(tid, oid, side, price, qty, quote=None, commission="0", asset="USDT", time=None):
    """构造一枚 myTrades 成交。side: "BUY"/"SELL" → isBuyer"""
    return {
        "id": tid,
        "orderId": oid,
        "isBuyer": side == "BUY",
        "price": str(price),
        "qty": str(qty),
        "quoteQty": str(quote if quote is not None else price * qty),
        "commission": commission,
        "commissionAsset": asset,
        "time": time if time is not None else tid * 1000,
    }


class TestBuildPlan:
    def test_position_and_lots_reconstructed(self):
        # 买入订单 B1 分两笔成交(2 @ 100), 卖出订单 S1 一笔(1 @ 110)
        trades = [
            _t(1, "B1", "BUY", 100.0, 1.0),
            _t(2, "B1", "BUY", 100.0, 1.0),
            _t(3, "S1", "SELL", 110.0, 1.0),
        ]
        plan = build_plan(trades, "SOLUSDT", cash_before=10000.0)
        assert plan.safe_mode is False
        assert plan.position["quantity"] == pytest.approx(1.0)
        assert plan.position["avg_price"] == pytest.approx(100.0)
        # FIFO 已实现盈亏 = (110-100)*1 = 10
        assert plan.position["realized_pnl"] == pytest.approx(10.0)
        # 1 个开仓 lot(剩余 1), 1 条卖出分配
        assert len(plan.buy_lots) == 1
        assert plan.buy_lots[0]["quantity"] == pytest.approx(1.0)
        assert len(plan.sell_allocations) == 1

    def test_fifo_realized_pnl_multiple_lots(self):
        # 两个买入 lot(1@100, 1@120), 卖出 1.5 @ 130 → FIFO 已实现盈亏
        trades = [
            _t(1, "B1", "BUY", 100.0, 1.0),
            _t(2, "B2", "BUY", 120.0, 1.0),
            _t(3, "S1", "SELL", 130.0, 1.5),
        ]
        plan = build_plan(trades, "SOLUSDT", cash_before=10000.0)
        # 30(来自 B1 全量) + 5(来自 B2 半量) = 35
        assert plan.position["realized_pnl"] == pytest.approx(35.0)
        assert plan.position["quantity"] == pytest.approx(0.5)
        assert plan.position["avg_price"] == pytest.approx(120.0)
        assert len(plan.sell_allocations) == 2  # 跨两个 lot

    def test_cash_and_ledger(self):
        trades = [
            _t(1, "B1", "BUY", 100.0, 1.0, commission="0.1", asset="USDT"),
            _t(2, "S1", "SELL", 110.0, 1.0, commission="0.1", asset="USDT"),
        ]
        plan = build_plan(trades, "SOLUSDT", cash_before=1000.0)
        # BUY: -100.1; SELL: +109.9 → 1009.8
        assert plan.cash_after == pytest.approx(1009.8)
        # 2 订单 × 2 资产 = 4 行
        assert len(plan.ledger) == 4
        # 现金守恒通过
        assert plan.conservation["cash_conservation"] is True
        assert plan.conservation["passed"] is True
        # 权益 = 现金 + 0 持仓
        assert plan.equity == pytest.approx(1009.8)

    def test_missing_cash_anchor_safe_mode(self):
        trades = [_t(1, "B1", "BUY", 100.0, 1.0)]
        plan = build_plan(trades, "SOLUSDT", cash_before=None)
        assert plan.safe_mode is True
        assert any("现金锚" in r for r in plan.reasons)
        # 持仓/lot 仍可重建(仅现金/账本/权益缺失)
        assert plan.position["quantity"] == pytest.approx(1.0)
        assert plan.cash_after is None

    def test_oversell_safe_mode(self):
        # 卖出量超过窗口内累计买入量(窗口起点晚于账户起始) → 超卖 → SAFE_MODE
        trades = [
            _t(1, "B1", "BUY", 100.0, 1.0),
            _t(2, "S1", "SELL", 110.0, 2.0),
        ]
        plan = build_plan(trades, "SOLUSDT", cash_before=1000.0)
        assert plan.safe_mode is True
        assert plan.conservation["no_oversell"] is False
        assert plan.conservation["base_conservation"] is False

    def test_unpriced_fee_not_safe_mode(self):
        # BNB 手续费: USDT 现金守恒不受影响, 但权益无法精确 → equity=None, 不 SAFE_MODE
        trades = [
            _t(1, "B1", "BUY", 100.0, 1.0, commission="0.003", asset="BNB"),
        ]
        plan = build_plan(trades, "SOLUSDT", cash_before=1000.0)
        assert plan.safe_mode is False
        assert plan.position["quantity"] == pytest.approx(1.0)
        # BNB 费不扣 USDT 现金: cash = 1000 - 100 = 900
        assert plan.cash_after == pytest.approx(900.0)
        assert plan.equity is None
        assert any("不可计价手续费" in r for r in plan.reasons)

    def test_idempotent(self):
        trades = [
            _t(1, "B1", "BUY", 100.0, 2.0, commission="0.2"),
            _t(2, "S1", "SELL", 105.0, 1.0, commission="0.1"),
        ]
        p1 = build_plan(trades, "SOLUSDT", cash_before=5000.0)
        p2 = build_plan(trades, "SOLUSDT", cash_before=5000.0)
        assert p1.position == p2.position
        assert p1.cash_after == p2.cash_after
        assert len(p1.buy_lots) == len(p2.buy_lots)
        assert len(p1.sell_allocations) == len(p2.sell_allocations)

    def test_dedup_and_sort(self):
        # 乱序 + 重复 id → 去重 + 按时间排序, 重建结果与有序一致
        trades = [
            _t(2, "S1", "SELL", 105.0, 1.0, time=2000),
            _t(1, "B1", "BUY", 100.0, 2.0, time=1000),
            _t(1, "B1", "BUY", 100.0, 2.0, time=1000),  # 重复
        ]
        plan = build_plan(trades, "SOLUSDT", cash_before=5000.0)
        assert len(plan.fills) == 2  # 去重后
        assert plan.position["quantity"] == pytest.approx(1.0)
        assert plan.safe_mode is False


class _FakeRest:
    def __init__(self, result):
        self.result = result

    async def get_my_trades_all(self, symbol, start_time=None, end_time=None,
                               limit=1000, max_pages=10):
        return self.result


class TestReconstructEngine:
    async def test_incomplete_history_safe_mode(self):
        rest = _FakeRest(MyTradesResult(
            trades=[_t(1, "B1", "BUY", 100.0, 1.0)],
            complete=False, pagination_exhausted=True, page_count=10,
        ))
        plan = await LedgerReconstructionEngine(rest).reconstruct(
            "SOLUSDT", cash_before=1000.0, dry_run=True,
        )
        assert plan.safe_mode is True
        assert any("不完整" in r for r in plan.reasons)

    async def test_no_rest_safe_mode(self):
        plan = await LedgerReconstructionEngine(None).reconstruct("SOLUSDT")
        assert plan.safe_mode is True

    async def test_dry_run_no_write(self, db_tables):
        from sqlalchemy import select

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import Position, PositionLot

        rest = _FakeRest(MyTradesResult(
            trades=[_t(1, "B1", "BUY", 100.0, 1.0)], complete=True,
        ))
        plan = await LedgerReconstructionEngine(rest).reconstruct(
            "SOLUSDT", cash_before=1000.0, dry_run=True,
        )
        assert plan.safe_mode is False
        async with AsyncSessionLocal() as session:
            lots = (await session.execute(select(PositionLot))).scalars().all()
            positions = (await session.execute(select(Position))).scalars().all()
        assert lots == []
        assert positions == []

    async def test_apply_writes_and_idempotent(self, db_tables):
        from sqlalchemy import select

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import OrderFill, Position, PositionLot, SellAllocation

        trades = [
            _t(1, "B1", "BUY", 100.0, 2.0, commission="0.2"),
            _t(2, "S1", "SELL", 105.0, 1.0, commission="0.1"),
        ]
        rest = _FakeRest(MyTradesResult(trades=trades, complete=True))
        engine = LedgerReconstructionEngine(rest)

        plan1 = await engine.reconstruct("SOLUSDT", cash_before=5000.0, dry_run=False)
        assert plan1.safe_mode is False

        async with AsyncSessionLocal() as session:
            lots1 = (await session.execute(select(PositionLot))).scalars().all()
            allocs1 = (await session.execute(select(SellAllocation))).scalars().all()
            fills1 = (await session.execute(select(OrderFill))).scalars().all()
            pos1 = (await session.execute(select(Position))).scalars().all()
        assert len(lots1) == 1
        assert len(allocs1) == 1
        assert len(fills1) == 2
        assert len(pos1) == 1
        assert pos1[0].quantity == pytest.approx(1.0)
        # 买入费摊入 lot 成本: 单位成本 (200+0.2)/2 = 100.1; 卖出 1 @ 105 →
        # 已实现 = (105-100.1)*1 - 卖出费 0.1 = 4.8
        assert pos1[0].realized_pnl == pytest.approx(4.8)

        # 幂等: 重复 apply → 终态一致(行数不变)
        await engine.reconstruct("SOLUSDT", cash_before=5000.0, dry_run=False)
        async with AsyncSessionLocal() as session:
            lots2 = (await session.execute(select(PositionLot))).scalars().all()
            allocs2 = (await session.execute(select(SellAllocation))).scalars().all()
            fills2 = (await session.execute(select(OrderFill))).scalars().all()
            pos2 = (await session.execute(select(Position))).scalars().all()
        assert len(lots2) == 1
        assert len(allocs2) == 1
        assert len(fills2) == 2
        assert len(pos2) == 1
        assert pos2[0].quantity == pytest.approx(1.0)
