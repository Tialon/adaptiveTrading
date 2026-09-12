"""
三维交叉对账(V10.4)

逐笔核对同一 client_order_id 在 Order / OrderFill / AccountLedger / PositionLot+SellAllocation
四个维度是否自洽:

- fill_coverage: Σ OrderFill.quantity == Order.filled_quantity, side 一致
- ledger_position(仅纸面): AccountLedger 的 base 资产行 change_amount == ±filled_quantity
  (AccountLedger 仅纸面模式落库; 实盘 cash_before=None 不写账本, SOL 持仓一致性由
  buy_lot/sell_alloc + lot 总和对账 + 权益对账兜底, 故实盘订单跳过此维度)
- buy_lot: BUY 订单的 lot「剩余量 + 已卖出量」== filled_quantity
- sell_alloc: SELL 订单的 Σ SellAllocation.quantity == filled_quantity

纯 DB 读, 不查交易所, 不改记账逻辑。任何一处对不上 -> 上层急停冻结。
(现金维度不做精确核对: 实盘 cash_after 仍是近似值, 已由 reconcile_account 权益对账兜底。)
"""

from datetime import datetime, timedelta, timezone
from typing import Any

from at01_common.logger import LoggerMixin
from at60_execution.reconciliation import _split_asset


def _naive_utc(dt: datetime) -> datetime:
    """去掉 tzinfo(SQLite 回读为 naive datetime, 与 cutoff 比较前统一口径)"""
    if dt is not None and dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


class CrossReconciler(LoggerMixin):
    """Order/Fill/Ledger/Lot 四维交叉对账器"""

    async def reconcile(
        self,
        symbol: str,
        window_seconds: float = 900.0,
        tolerance: float = 1e-6,
    ) -> list[dict[str, Any]]:
        """核对近期订单(实盘+纸面)的四维一致性, 返回差异列表(空 = 自洽)"""
        from sqlalchemy import select

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import Order

        cutoff = _naive_utc(datetime.now(timezone.utc)) - timedelta(seconds=window_seconds)
        base, _quote = _split_asset(symbol)
        mismatches: list[dict[str, Any]] = []
        try:
            async with AsyncSessionLocal() as session:
                orders = (
                    await session.execute(
                        select(Order)
                        .where(Order.symbol == symbol)
                        .order_by(Order.id.desc())
                        .limit(200)
                    )
                ).scalars().all()
                for o in orders:
                    if o.created_at is not None and _naive_utc(o.created_at) < cutoff:
                        continue
                    if o.filled_quantity is None or o.filled_quantity <= 0:
                        continue
                    mismatches.extend(await self._check_order(session, o, base, tolerance))
        except Exception as e:
            self.logger.exception("交叉对账失败", symbol=symbol)
            return [{"type": "cross_reconcile_error", "symbol": symbol, "detail": str(e)}]
        return mismatches

    # ---------- 内部 ----------

    async def _check_order(self, session, order, base: str, tolerance: float) -> list[dict[str, Any]]:
        from sqlalchemy import func, select

        from at01_common.models import AccountLedger, OrderFill, PositionLot, SellAllocation

        cid = order.client_order_id
        side = (order.side or "").upper()
        filled = order.filled_quantity or 0.0
        mismatches: list[dict[str, Any]] = []

        # 1. fill coverage + side
        fills = (await session.execute(
            select(OrderFill).where(OrderFill.client_order_id == cid)
        )).scalars().all()
        if not fills:
            mismatches.append(self._m(order, "fill_missing", filled, 0.0))
        else:
            fill_sum = sum(f.quantity or 0.0 for f in fills)
            if abs(fill_sum - filled) > tolerance:
                mismatches.append(self._m(order, "fill_mismatch", filled, fill_sum))
            for f in fills:
                if (f.side or "").upper() != side:
                    mismatches.append(self._m(order, "fill_side_mismatch", side, f.side))
                    break

        # 3. ledger position(base 资产行 change_amount == ±filled) —— 仅纸面订单。
        #    实盘模式 AccountLedger 不落库(cash_before=None), SOL 持仓一致性已由
        #    buy_lot/sell_alloc + lot 总和对账 + 权益对账兜底, 此处跳过避免误报 ledger_missing。
        if order.is_paper:
            ledger_rows = (await session.execute(
                select(AccountLedger).where(
                    AccountLedger.related_order_id == cid,
                    AccountLedger.asset == base,
                )
            )).scalars().all()
            if not ledger_rows:
                mismatches.append(self._m(order, "ledger_missing", filled, 0.0))
            else:
                pos_change = sum(r.change_amount or 0.0 for r in ledger_rows)
                expected_change = filled if side == "BUY" else -filled
                if abs(pos_change - expected_change) > tolerance:
                    mismatches.append(
                        self._m(order, "ledger_position_mismatch", expected_change, pos_change)
                    )

        # 4/5. lot / sell allocation
        if side == "BUY":
            lots = (await session.execute(
                select(PositionLot).where(PositionLot.client_order_id == cid)
            )).scalars().all()
            if not lots:
                mismatches.append(self._m(order, "buy_lot_mismatch", filled, 0.0))
            else:
                total_original = 0.0
                for lot in lots:
                    sold = (await session.execute(
                        select(func.coalesce(func.sum(SellAllocation.quantity), 0.0))
                        .where(SellAllocation.lot_id == lot.id)
                    )).scalar()
                    # 已清 lot 的剩余量应为 0(卖出时落库归零); 即使历史数据残留非零
                    # quantity, 这里也按 0 计, 只以 SellAllocation 反推原始买入量, 避免误报。
                    remaining = 0.0 if (lot.status or "") == "closed" else (lot.quantity or 0.0)
                    total_original += remaining + float(sold or 0.0)
                if abs(total_original - filled) > tolerance:
                    mismatches.append(self._m(order, "buy_lot_mismatch", filled, total_original))
        else:  # SELL
            alloc_sum = (await session.execute(
                select(func.coalesce(func.sum(SellAllocation.quantity), 0.0))
                .where(SellAllocation.sell_client_order_id == cid)
            )).scalar()
            alloc_sum = float(alloc_sum or 0.0)
            if abs(alloc_sum - filled) > tolerance:
                mismatches.append(self._m(order, "sell_alloc_mismatch", filled, alloc_sum))

        return mismatches

    def _m(self, order, type_: str, expected: Any, actual: Any) -> dict[str, Any]:
        return {
            "type": type_,
            "symbol": order.symbol,
            "client_order_id": order.client_order_id,
            "expected": expected,
            "actual": actual,
        }
