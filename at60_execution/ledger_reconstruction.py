"""账本重建引擎(V11.1 P0-3)

从交易所真相(myTrades 全量成交历史)重建本地账务状态:

    Exchange Truth → Trades(OrderFill) → Orders(订单级分组) → Buy Lots(PositionLot)
    → Sell Allocations(SellAllocation) → Position → Cash → Ledger → Equity

设计要点:
- **幂等**: 纯函数 `build_plan` 对同一批成交输出相同计划; `apply` 单事务「先清后插」,
  重复执行得到相同终态。
- **dry-run + apply**: `dry_run=True` 只产出计划不落库; `dry_run=False` 且无歧义才落库。
- **守恒检查**: base 守恒(买入量 == 卖出量 + 开仓量)/ 无超卖 / 卖出分配覆盖 / 现金守恒(有现金锚时),
  任一失败 → SAFE_MODE(拒绝 apply)。
- **SAFE_MODE(歧义拒绝 apply)**: 成交历史不完整(分页截断) / 缺现金锚 / 守恒失败 /
  同订单成交方向不一致 / 交易所客户端不可用。

边界与口径:
- 现金锚(cash_before)是重建窗口起点现金, 交易所成交历史本身只有「现金变动」, 无法还原绝对现金,
  故现金/账本/权益重建必须有 cash_before, 缺失即 SAFE_MODE(不猜)。
- 不可计价手续费(非 USDT/SOL, 如 BNB): USDT 现金守恒不受影响(费从第三方资产扣), 但权益无法精确
  计算 → 标记 reason 且 equity=None, 不触发 SAFE_MODE(持仓数量/lot 仍精确)。
- 只重建账务状态表(OrderFill / PositionLot / SellAllocation / Position); `orders` 表不重建
  (client_order_id 是本地幂等键, 交易所真相无此信息); `account_ledger` 为 append-only 审计不重建
  (计划里的 ledger 是投影视图, 供守恒检查与权益估算, 不落库)。
"""

from dataclasses import dataclass, field
from typing import Any, Optional

from at01_common.logger import LoggerMixin
from at60_execution.fee_calculator import FeeCalculator

_EPS = 1e-9


@dataclass
class ReconstructionOrder:
    """订单级分组(同一 exchange_order_id 的成交聚合)"""

    exchange_order_id: str
    side: str
    qty: float
    quote_qty: float
    fee_quote: float
    fee_unpriced: bool
    first_time: int
    fills: list = field(default_factory=list)


@dataclass
class ReconstructionPlan:
    """重建计划(dry-run 输出 / apply 输入)"""

    symbol: str = ""
    base: str = "SOL"
    quote: str = "USDT"
    safe_mode: bool = False
    reasons: list = field(default_factory=list)
    orders: list = field(default_factory=list)
    fills: list = field(default_factory=list)
    buy_lots: list = field(default_factory=list)
    sell_allocations: list = field(default_factory=list)
    position: dict = field(default_factory=lambda: {
        "quantity": 0.0, "avg_price": 0.0, "realized_pnl": 0.0,
    })
    ledger: list = field(default_factory=list)
    cash_before: Optional[float] = None
    cash_after: Optional[float] = None
    equity: Optional[float] = None
    conservation: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """可序列化视图(dry-run 报告 / 日志)"""
        return {
            "symbol": self.symbol,
            "safe_mode": self.safe_mode,
            "reasons": self.reasons,
            "orders": [
                {
                    "exchange_order_id": o.exchange_order_id, "side": o.side,
                    "qty": o.qty, "quote_qty": o.quote_qty, "fee_quote": o.fee_quote,
                    "fee_unpriced": o.fee_unpriced, "first_time": o.first_time,
                }
                for o in self.orders
            ],
            "fill_count": len(self.fills),
            "buy_lot_count": len(self.buy_lots),
            "sell_alloc_count": len(self.sell_allocations),
            "position": self.position,
            "ledger_rows": len(self.ledger),
            "cash_before": self.cash_before,
            "cash_after": self.cash_after,
            "equity": self.equity,
            "conservation": self.conservation,
        }


# ---------- 纯算法(无 I/O, 便于单测) ----------


def _dedup_sort(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按成交 id 去重(保留首次出现), 按时间/成交 id 升序排序"""
    seen: set = set()
    uniq: list[dict[str, Any]] = []
    for t in trades:
        tid = t.get("id")
        if tid is None:
            continue
        if tid in seen:
            continue
        seen.add(tid)
        uniq.append(t)
    uniq.sort(key=lambda t: (int(t.get("time", 0) or 0), int(t.get("id", 0) or 0)))
    return uniq


def _group_orders(
    fills: list[dict[str, Any]], symbol: str
) -> tuple[list[ReconstructionOrder], list[str]]:
    """按 exchange_order_id 聚合成交 → 订单级分组(方向取 isBuyer)。

    返回 (订单列表按成交时间升序, 歧义原因)。同一订单内成交方向不一致即歧义。
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for f in fills:
        oid = str(f.get("orderId") or "")
        if oid:
            groups.setdefault(oid, []).append(f)

    calc = FeeCalculator(symbol)
    orders: list[ReconstructionOrder] = []
    reasons: list[str] = []
    for oid, fs in groups.items():
        sides = {"BUY" if f.get("isBuyer") else "SELL" for f in fs}
        if len(sides) != 1:
            reasons.append(f"订单 {oid} 成交方向不一致 {sorted(sides)}")
            continue
        side = sides.pop()
        qty = sum(float(f.get("qty", 0) or 0) for f in fs)
        quote_qty = sum(float(f.get("quoteQty", 0) or 0) for f in fs)
        fee = calc.total(fs)
        first_time = min(int(f.get("time", 0) or 0) for f in fs)
        orders.append(ReconstructionOrder(
            exchange_order_id=oid, side=side, qty=qty, quote_qty=quote_qty,
            fee_quote=fee.fee_quote, fee_unpriced=fee.unpriced,
            first_time=first_time, fills=fs,
        ))

    orders.sort(key=lambda o: (o.first_time, o.exchange_order_id))
    return orders, reasons


def _replay_fifo(
    orders: list[ReconstructionOrder], symbol: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], float]:
    """FIFO 回放: 逐订单建 lot / 消费 lot, 返回 (开仓 lot, 卖出分配, 持仓, 未匹配卖出量)。

    未匹配卖出量 > 0 表示卖出量超过窗口内累计买入量(重建窗口起点晚于账户起始),
    属歧义 → 守恒检查失败 → SAFE_MODE。
    """
    lots: list[dict[str, Any]] = []
    sell_allocs: list[dict[str, Any]] = []
    realized_total = 0.0
    unmatched_sell = 0.0

    for o in orders:
        if o.side == "BUY":
            unit_cost = (o.quote_qty + o.fee_quote) / o.qty if o.qty > 0 else 0.0
            lots.append({
                "symbol": symbol,
                "exchange_order_id": o.exchange_order_id,
                "quantity": o.qty,
                "price": unit_cost,
                "fee_quote": o.fee_quote,
            })
        else:
            sell_price = o.quote_qty / o.qty if o.qty > 0 else 0.0
            remaining = o.qty
            gross = 0.0
            for lot in lots:
                if remaining <= _EPS:
                    break
                if lot["quantity"] <= _EPS:
                    continue
                alloc_qty = min(remaining, lot["quantity"])
                lot_realized = (sell_price - lot["price"]) * alloc_qty
                gross += lot_realized
                sell_allocs.append({
                    "symbol": symbol,
                    "sell_exchange_order_id": o.exchange_order_id,
                    "buy_exchange_order_id": lot["exchange_order_id"],
                    "quantity": alloc_qty,
                    "lot_price": lot["price"],
                    "sell_price": sell_price,
                    "realized_pnl": lot_realized,
                })
                lot["quantity"] -= alloc_qty
                remaining -= alloc_qty
                if lot["quantity"] <= _EPS:
                    lot["quantity"] = 0.0
            unmatched_sell += remaining
            realized_total += gross - o.fee_quote

    open_lots = [l for l in lots if l["quantity"] > _EPS]
    position_qty = sum(l["quantity"] for l in open_lots)
    position_avg = (
        sum(l["quantity"] * l["price"] for l in open_lots) / position_qty
        if position_qty > 0 else 0.0
    )
    position = {
        "quantity": position_qty,
        "avg_price": position_avg,
        "realized_pnl": realized_total,
    }
    return open_lots, sell_allocs, position, unmatched_sell


def _replay_cash(
    orders: list[ReconstructionOrder], symbol: str, cash_before: float, base: str, quote: str
) -> tuple[float, list[dict[str, Any]]]:
    """按成交顺序回放现金/持仓余额, 产出账本视图(每订单 USDT + SOL 两行)。"""
    cash = cash_before
    pos = 0.0
    ledger: list[dict[str, Any]] = []
    for o in orders:
        cash_b = cash
        pos_b = pos
        if o.side == "BUY":
            cash -= (o.quote_qty + o.fee_quote)
            pos += o.qty
        else:
            cash += (o.quote_qty - o.fee_quote)
            pos -= o.qty
        for asset, before, change, after in (
            (quote, cash_b, cash - cash_b, cash),
            (base, pos_b, pos - pos_b, pos),
        ):
            ledger.append({
                "symbol": symbol, "side": o.side, "asset": asset,
                "before": before, "change": change, "after": after,
                "commission": o.fee_quote, "related_order_id": o.exchange_order_id,
            })
    return cash, ledger


def _conservation_checks(
    orders: list[ReconstructionOrder],
    open_lots: list[dict[str, Any]],
    sell_allocs: list[dict[str, Any]],
    position: dict[str, Any],
    unmatched_sell: float,
    cash_before: Optional[float],
    cash_after: Optional[float],
) -> dict[str, Any]:
    """守恒检查。cash_before 缺失时现金守恒不评估(None)。"""
    buy_qty = sum(o.qty for o in orders if o.side == "BUY")
    sell_qty = sum(o.qty for o in orders if o.side == "SELL")
    open_qty = position["quantity"]
    alloc_qty = sum(a["quantity"] for a in sell_allocs)

    checks: dict[str, Any] = {
        "no_oversell": unmatched_sell <= _EPS,
        "base_conservation": abs(buy_qty - (sell_qty + open_qty)) <= _EPS,
        "lot_conservation": abs(open_qty - sum(l["quantity"] for l in open_lots)) <= _EPS,
        "sell_alloc_coverage": abs(alloc_qty - (sell_qty - unmatched_sell)) <= _EPS,
    }
    if cash_before is not None:
        expected = cash_before \
            - sum(o.quote_qty + o.fee_quote for o in orders if o.side == "BUY") \
            + sum(o.quote_qty - o.fee_quote for o in orders if o.side == "SELL")
        checks["cash_conservation"] = abs((cash_after or 0.0) - expected) <= _EPS
    else:
        checks["cash_conservation"] = None

    evaluated = [v for v in checks.values() if v is not None]
    checks["passed"] = all(evaluated)
    return checks


def build_plan(
    trades: list[dict[str, Any]],
    symbol: str,
    cash_before: Optional[float] = None,
    base: str = "SOL",
    quote: str = "USDT",
    last_price: Optional[float] = None,
) -> ReconstructionPlan:
    """从成交历史重建账务状态(纯函数, 不落库)。"""
    reasons: list[str] = []
    fills = _dedup_sort(trades)
    orders, mixed = _group_orders(fills, symbol)
    reasons.extend(mixed)

    buy_lots, sell_allocs, position, unmatched_sell = _replay_fifo(orders, symbol)

    cash_after: Optional[float] = None
    ledger: list[dict[str, Any]] = []
    if cash_before is None:
        reasons.append("缺少现金锚(cash_before), 无法重建现金/账本/权益")
    else:
        cash_after, ledger = _replay_cash(orders, symbol, cash_before, base, quote)

    conservation = _conservation_checks(
        orders, buy_lots, sell_allocs, position, unmatched_sell, cash_before, cash_after,
    )

    unpriced = any(o.fee_unpriced for o in orders)
    if unpriced:
        reasons.append("存在不可计价手续费(非 USDT/SOL), 权益无法精确计算")

    equity: Optional[float] = None
    if cash_after is not None and not unpriced:
        ref_price = last_price
        if ref_price is None and orders:
            last = orders[-1]
            ref_price = last.quote_qty / last.qty if last.qty > 0 else 0.0
        if ref_price is not None:
            equity = cash_after + position["quantity"] * ref_price

    safe_mode = cash_before is None or not conservation["passed"] or bool(mixed)

    return ReconstructionPlan(
        symbol=symbol, base=base, quote=quote, safe_mode=safe_mode, reasons=reasons,
        orders=orders, fills=fills, buy_lots=buy_lots, sell_allocations=sell_allocs,
        position=position, ledger=ledger, cash_before=cash_before, cash_after=cash_after,
        equity=equity, conservation=conservation,
    )


# ---------- 引擎(拉取 + 可选落库) ----------


class LedgerReconstructionEngine(LoggerMixin):
    """账本重建引擎: 拉交易所真相 → 重建计划 → (可选)落库。"""

    def __init__(self, rest_client: Any = None):
        self.rest = rest_client

    async def reconstruct(
        self,
        symbol: str,
        *,
        cash_before: Optional[float] = None,
        dry_run: bool = True,
        start_time_ms: Optional[int] = None,
        last_price: Optional[float] = None,
        base: str = "SOL",
        quote: str = "USDT",
    ) -> ReconstructionPlan:
        """拉取成交历史并重建。dry_run=True 只产出计划; False 且无歧义才 apply。"""
        if self.rest is None:
            return ReconstructionPlan(
                symbol=symbol, base=base, quote=quote, safe_mode=True,
                reasons=["无交易所客户端, 无法拉取成交历史"],
            )

        try:
            get_all = getattr(self.rest, "get_my_trades_all", None)
            if get_all is not None:
                result = await get_all(symbol, start_time=start_time_ms)
                trades = result.trades if hasattr(result, "trades") else result
                complete = result.complete if hasattr(result, "trades") else True
            else:
                trades = await self.rest.get_my_trades(symbol, limit=1000)
                complete = True
        except Exception as e:
            self.logger.warning("账本重建拉取成交历史失败", symbol=symbol, error=str(e))
            return ReconstructionPlan(
                symbol=symbol, base=base, quote=quote, safe_mode=True,
                reasons=[f"成交历史获取失败: {e}"],
            )

        if not complete:
            return ReconstructionPlan(
                symbol=symbol, base=base, quote=quote, safe_mode=True,
                reasons=["成交历史不完整(分页截断), 拒绝重建"],
            )

        plan = build_plan(
            trades, symbol, cash_before=cash_before, base=base, quote=quote,
            last_price=last_price,
        )

        if plan.safe_mode:
            self.logger.warning(
                "账本重建拒绝 apply(SAFE_MODE)", symbol=symbol, reasons=plan.reasons,
            )
        elif not dry_run:
            await self._apply(plan)
            self.logger.info("账本重建已 apply", **plan.to_dict())

        return plan

    async def _apply(self, plan: ReconstructionPlan) -> None:
        """单事务「先清后插」落库(幂等: 重复 apply 得到相同终态)。"""
        from sqlalchemy import delete

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import OrderFill, Position, PositionLot, SellAllocation

        calc = FeeCalculator(plan.symbol)
        async with AsyncSessionLocal() as session:
            await session.execute(delete(SellAllocation).where(SellAllocation.symbol == plan.symbol))
            await session.execute(delete(PositionLot).where(PositionLot.symbol == plan.symbol))
            await session.execute(delete(OrderFill).where(OrderFill.symbol == plan.symbol))
            await session.execute(delete(Position).where(Position.symbol == plan.symbol))

            lot_id_map: dict[str, int] = {}
            for l in plan.buy_lots:
                row = PositionLot(
                    symbol=plan.symbol,
                    quantity=l["quantity"],
                    price=l["price"],
                    fee_quote=l["fee_quote"],
                    client_order_id=None,
                    exchange_order_id=l.get("exchange_order_id"),
                    status="open",
                )
                session.add(row)
                await session.flush()
                lot_id_map[l["exchange_order_id"]] = row.id

            for a in plan.sell_allocations:
                session.add(SellAllocation(
                    symbol=plan.symbol,
                    sell_client_order_id="",
                    sell_exchange_order_id=a.get("sell_exchange_order_id"),
                    lot_id=lot_id_map.get(a.get("buy_exchange_order_id")),
                    quantity=a["quantity"],
                    lot_price=a["lot_price"],
                    sell_price=a["sell_price"],
                    realized_pnl=a["realized_pnl"],
                ))

            for f in plan.fills:
                tid = f.get("id")
                tid_int = int(tid) if tid is not None else None
                eoid = str(f.get("orderId") or "")
                ff = calc.fill_fee(f)
                session.add(OrderFill(
                    order_id=None,
                    client_order_id="",
                    exchange_order_id=eoid,
                    exchange_trade_id=tid_int,
                    fill_idempotency_key=f"{eoid}:{tid_int if tid_int is not None else 'na'}",
                    symbol=plan.symbol,
                    side="BUY" if f.get("isBuyer") else "SELL",
                    price=float(f.get("price", 0) or 0),
                    quantity=float(f.get("qty", 0) or 0),
                    quote_quantity=float(f.get("quoteQty", 0) or 0),
                    commission=ff.commission,
                    commission_asset=ff.commission_asset,
                    fee_quote=ff.fee_quote,
                    fee_valuation_status=ff.valuation_status,
                    trade_time=int(f.get("time", 0) or 0),
                ))

            session.add(Position(
                symbol=plan.symbol,
                quantity=plan.position["quantity"],
                avg_price=plan.position["avg_price"],
                realized_pnl=plan.position["realized_pnl"],
                peak_price=0.0,
            ))

            await session.commit()
