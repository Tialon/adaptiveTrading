"""
Lot 会计(V10.3)

FIFO 开仓批次追踪 —— 附加审计层:

- 逐笔买入 = 一个 lot(单位成本摊入买入费), 追加到 FIFO 队列
- 逐笔卖出按 FIFO 消费 lot, 记录 SellAllocation + 精确已实现盈亏
- 不动平均成本的 PositionState(风险/equity 口径不变), 本模块仅作
  审计/报告口径 + 「开仓 lot 总和 == 持仓量」对账不变量

DB 写入尽力而为(异常仅记日志, 不阻断成交主路径, 与 ExecutionAttempt 同)。
"""

from typing import Any, Optional

from at01_common.logger import LoggerMixin

# 浮点清零阈值(lot 剩余量)
_EPS = 1e-9


class LotTracker(LoggerMixin):
    """FIFO 批次追踪器"""

    def __init__(self):
        self.lots: dict[str, list[dict[str, Any]]] = {}  # symbol -> FIFO 队列(open lots)

    # ---------- 记账 ----------

    async def add_buy(
        self,
        symbol: str,
        qty: float,
        price: float,
        fee_quote: float = 0.0,
        client_order_id: str = "",
        exchange_order_id: Optional[str] = None,
        session=None,
    ) -> dict[str, Any]:
        """买入成交: 追加一个 lot(买入费摊入单位成本), 落库 PositionLot

        session: 传入时复用该会话(不提交、异常上抛, 供外部强一致事务); 否则尽力而为。
        """
        if qty <= 0:
            return {}
        unit_cost = (qty * price + fee_quote) / qty if qty > 0 else 0.0
        lot = {
            "id": None,  # PositionLot.id, 落库成功后回填
            "symbol": symbol,
            "quantity": qty,
            "price": unit_cost,
            "fee_quote": fee_quote,
            "client_order_id": client_order_id,
            "exchange_order_id": exchange_order_id,
        }
        self.lots.setdefault(symbol, []).append(lot)
        try:
            lot["id"] = await self._insert_lot(lot, session=session)
        except Exception:
            if session is None:
                self.logger.exception("PositionLot 落库失败", symbol=symbol)
            else:
                raise
        return lot

    async def allocate_sell(
        self,
        symbol: str,
        qty: float,
        price: float,
        fee_quote: float = 0.0,
        client_order_id: str = "",
        exchange_order_id: Optional[str] = None,
        session=None,
    ) -> tuple[float, float, list[dict[str, Any]]]:
        """卖出成交: FIFO 消费 lot, 返回 (已实现盈亏, 匹配成本, 分配明细)

        - realized = Σ(price - lot.price)*alloc_qty - fee_quote(卖出费一次性扣)
        - matched_cost = Σ(lot.price * alloc_qty)
        - 卖出量超开仓量时按可卖量截断并告警(对齐 apply_sell 的 min 语义)
        - session: 传入时复用该会话(不提交、异常上抛, 供外部强一致事务)
        """
        lots = self.lots.setdefault(symbol, [])
        available = sum(l["quantity"] for l in lots if l["quantity"] > 0)
        if qty > available:
            self.logger.warning(
                "卖出量超开仓 lot 总和, 按可卖量截断", symbol=symbol, qty=qty, available=available,
            )
        remaining = min(qty, available)

        realized_gross = 0.0
        matched_cost = 0.0
        allocations: list[dict[str, Any]] = []
        closed_lot_ids: list[Optional[int]] = []

        for lot in lots:
            if remaining <= 0:
                break
            if lot["quantity"] <= 0:
                continue
            alloc_qty = min(remaining, lot["quantity"])
            lot_realized = (price - lot["price"]) * alloc_qty
            realized_gross += lot_realized
            matched_cost += lot["price"] * alloc_qty
            allocations.append({
                "lot_id": lot["id"],
                "quantity": alloc_qty,
                "lot_price": lot["price"],
                "sell_price": price,
                "realized_pnl": lot_realized,
            })
            lot["quantity"] -= alloc_qty
            remaining -= alloc_qty
            if lot["quantity"] < _EPS:
                lot["quantity"] = 0.0
                closed_lot_ids.append(lot["id"])

        # 移除已清 lot, 保留仍开仓的
        self.lots[symbol] = [l for l in lots if l["quantity"] > _EPS]

        realized = realized_gross - fee_quote
        try:
            await self._persist_allocations(
                symbol, client_order_id, exchange_order_id,
                allocations, closed_lot_ids, self.lots[symbol],
                session=session,
            )
        except Exception:
            if session is None:
                self.logger.exception("SellAllocation 落库失败", symbol=symbol)
            else:
                raise
        return realized, matched_cost, allocations

    # ---------- 查询 / 对账 ----------

    def open_quantity(self, symbol: str) -> float:
        """开仓 lot 总数量"""
        return sum(l["quantity"] for l in self.lots.get(symbol, []))

    def remaining_cost_basis(self, symbol: str) -> float:
        """开仓 lot 剩余成本基础(Σ 数量 × 单位成本)"""
        return sum(l["quantity"] * l["price"] for l in self.lots.get(symbol, []))

    def reconcile(
        self, symbol: str, position_quantity: float, tolerance: float = 1e-6
    ) -> Optional[dict[str, Any]]:
        """对账不变量: 开仓 lot 总和应等于 PositionState.quantity; 超容差返回 diff"""
        open_qty = self.open_quantity(symbol)
        diff = position_quantity - open_qty
        if abs(diff) > tolerance:
            return {
                "symbol": symbol,
                "position_quantity": position_quantity,
                "lot_quantity": open_qty,
                "diff": diff,
            }
        return None

    # ---------- 持久化 ----------

    async def _insert_lot(self, lot: dict[str, Any], session=None) -> Optional[int]:
        from sqlalchemy import select

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import PositionLot

        def _make() -> PositionLot:
            return PositionLot(
                symbol=lot["symbol"],
                quantity=lot["quantity"],
                price=lot["price"],
                fee_quote=lot["fee_quote"],
                client_order_id=lot["client_order_id"] or None,
                exchange_order_id=lot["exchange_order_id"],
                status="open",
            )

        cid = lot["client_order_id"] or None

        async def _find_existing(s) -> Optional[PositionLot]:
            if not cid:
                return None
            return (
                await s.execute(
                    select(PositionLot).where(PositionLot.client_order_id == cid)
                )
            ).scalars().first()

        # V11.0(F12): 幂等 —— 同 client_order_id 的 lot 已存在则复用其 id(不重复记账)。
        # 唯一约束(client_order_id)为兜底硬约束; 此检查使崩溃窗口恢复的重放安全跳过。
        if session is not None:
            existing = await _find_existing(session)
            if existing is not None:
                return existing.id
            row = _make()
            session.add(row)
            await session.flush()  # 取回自增 id, 供后续 SellAllocation 引用
            return row.id

        try:
            async with AsyncSessionLocal() as s:
                existing = await _find_existing(s)
                if existing is not None:
                    return existing.id
                row = _make()
                s.add(row)
                await s.commit()
            return row.id
        except Exception:
            self.logger.exception("PositionLot 落库失败", symbol=lot["symbol"])
            return None

    async def _persist_allocations(
        self,
        symbol: str,
        sell_client_order_id: str,
        exchange_order_id: Optional[str],
        allocations: list[dict[str, Any]],
        closed_lot_ids: list[Optional[int]],
        open_lots: list[dict[str, Any]],
        session=None,
    ) -> None:
        from sqlalchemy import update

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import PositionLot, SellAllocation

        async def _write(s) -> None:
            for a in allocations:
                s.add(SellAllocation(
                    symbol=symbol,
                    sell_client_order_id=sell_client_order_id or "",
                    sell_exchange_order_id=exchange_order_id,
                    lot_id=a["lot_id"],
                    quantity=a["quantity"],
                    lot_price=a["lot_price"],
                    sell_price=a["sell_price"],
                    realized_pnl=a["realized_pnl"],
                ))
            for lid in closed_lot_ids:
                if lid is not None:
                    await s.execute(
                        update(PositionLot).where(PositionLot.id == lid).values(
                            status="closed", quantity=0.0
                        )
                    )
            for l in open_lots:
                if l["id"] is not None:
                    await s.execute(
                        update(PositionLot).where(PositionLot.id == l["id"]).values(quantity=l["quantity"])
                    )

        if session is not None:
            await _write(session)
            return

        try:
            async with AsyncSessionLocal() as s:
                await _write(s)
                await s.commit()
        except Exception:
            self.logger.exception("SellAllocation 落库失败", symbol=symbol)

    async def load_from_db(self) -> None:
        """启动时加载开仓 lot(崩溃后恢复 FIFO 队列)"""
        from sqlalchemy import select

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import PositionLot

        try:
            async with AsyncSessionLocal() as session:
                rows = (
                    await session.execute(
                        select(PositionLot)
                        .where(PositionLot.status == "open")
                        .order_by(PositionLot.id)
                    )
                ).scalars().all()
                for r in rows:
                    self.lots.setdefault(r.symbol, []).append({
                        "id": r.id,
                        "symbol": r.symbol,
                        "quantity": r.quantity,
                        "price": r.price,
                        "fee_quote": r.fee_quote,
                        "client_order_id": r.client_order_id,
                        "exchange_order_id": r.exchange_order_id,
                    })
            total = sum(len(v) for v in self.lots.values())
            self.logger.info("开仓 lot 已加载", count=total)
        except Exception:
            self.logger.exception("开仓 lot 加载失败")
