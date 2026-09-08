"""
Portfolio Engine(V3.0)

持仓成本管理 —— 核心理念: **卖出的目的不是减少仓位, 而是降低成本**。

职责:
- 卖出降本计算: 盈利卖出 -> 剩余持仓成本下降
- 成本曲线记录(每次成交后的成本变化)
- 目标仓位与偏离
- 加仓均价 / 可买可卖额度聚合

与 PositionManager 的关系:
- PositionManager 负责"账务"(数量/均价/盈亏的记账)
- PortfolioEngine 负责"策略"(目标仓位、降本路径、再平衡建议)
"""

from dataclasses import dataclass, field
from typing import Any

from at60_risk.risk_position import PositionManager


@dataclass
class CostEvent:
    """成本变化事件"""

    ts: float
    symbol: str
    kind: str  # buy / sell / reset
    qty: float
    price: float
    cost_before: float  # 事件前平均成本
    cost_after: float  # 事件后平均成本
    realized: float = 0.0  # 本事件已实现盈亏

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts, "kind": self.kind, "qty": self.qty, "price": self.price,
            "cost_before": round(self.cost_before, 4),
            "cost_after": round(self.cost_after, 4),
            "realized": round(self.realized, 4),
        }


@dataclass
class PortfolioView:
    """组合视图(供决策/展示)"""

    symbol: str
    quantity: float = 0.0
    avg_cost: float = 0.0
    market_price: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    total_pnl: float = 0.0
    cost_reduction: float = 0.0  # 累计降本(初始成本 - 当前成本)
    cost_curve: list[dict[str, Any]] = field(default_factory=list)
    breakeven_price: float = 0.0  # 保本价(考虑已实现盈亏摊薄)
    sell_to_breakeven_qty: float = 0.0  # 当前价下卖出多少即回本

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "quantity": self.quantity,
            "avg_cost": round(self.avg_cost, 4),
            "market_price": self.market_price,
            "unrealized_pnl": round(self.unrealized_pnl, 2),
            "realized_pnl": round(self.realized_pnl, 2),
            "total_pnl": round(self.total_pnl, 2),
            "cost_reduction": round(self.cost_reduction, 4),
            "breakeven_price": round(self.breakeven_price, 4),
            "cost_curve_len": len(self.cost_curve),
        }


class PortfolioEngine:
    """组合/成本管理引擎"""

    def __init__(self, positions: PositionManager):
        self.positions = positions
        # 成本事件历史(内存, 最近 N 条)
        self.cost_events: dict[str, list[CostEvent]] = {}
        self._initial_cost: dict[str, float] = {}  # 首次建仓成本(降本基准)

    # ---------- 事件钩子(由执行引擎在成交后调用) ----------

    def on_buy_fill(self, symbol: str, qty: float, price: float, fee: float = 0.0) -> None:
        """买入成交 -> 均价上移(买入费摊入单位成本, 与 FIFO lot 口径一致)"""
        import time

        pos = self.positions.get(symbol)
        before = pos.avg_price
        self.positions.apply_buy(symbol, qty, price, fee)
        after = self.positions.get(symbol).avg_price
        if symbol not in self._initial_cost:
            self._initial_cost[symbol] = after
        self._record(symbol, CostEvent(
            ts=time.time(), symbol=symbol, kind="buy",
            qty=qty, price=price, cost_before=before, cost_after=after,
        ))

    def on_sell_fill(self, symbol: str, qty: float, price: float, fee: float = 0.0) -> tuple[float, float]:
        """卖出成交 -> 降本计算

        返回 (已实现盈亏, 新成本)。核心:
        - 盈利卖出: 剩余持仓成本下降(利润实质上是"现金已落袋", 摊薄持仓成本)
        - 亏损卖出: 成本上升(补亏损)
        """
        import time

        pos = self.positions.get(symbol)
        before = pos.avg_price
        _, realized = self.positions.apply_sell(symbol, qty, price, fee)
        after = self.positions.get(symbol).avg_price

        self._record(symbol, CostEvent(
            ts=time.time(), symbol=symbol, kind="sell",
            qty=qty, price=price, cost_before=before, cost_after=after,
            realized=realized,
        ))
        return realized, after

    # ---------- 查询 ----------

    def view(self, symbol: str, market_price: float) -> PortfolioView:
        """组合视图"""
        pos = self.positions.get(symbol)
        events = self.cost_events.get(symbol, [])
        initial = self._initial_cost.get(symbol, pos.avg_price)

        unrealized = self.positions.unrealized_pnl(symbol, market_price)
        realized = pos.realized_pnl

        # 保本价: 若把已实现盈亏视为"从持仓中拿走的钱",
        # 当前持仓的保本成本 = avg_price - realized_pnl / quantity
        breakeven = pos.avg_price
        if pos.quantity > 0:
            breakeven = pos.avg_price - (realized / pos.quantity)

        sell_to_be = 0.0
        if market_price > breakeven > 0 and pos.quantity > 0:
            # 盈利状态下: 卖多少能把剩余成本降到市价(全部回本)
            gap = pos.avg_price - market_price
            if gap > 0:
                sell_to_be = min(
                    pos.quantity,
                    (realized + gap * pos.quantity)
                    / max(market_price - (breakeven - market_price), 1e-9),
                )

        return PortfolioView(
            symbol=symbol,
            quantity=pos.quantity,
            avg_cost=pos.avg_price,
            market_price=market_price,
            unrealized_pnl=unrealized,
            realized_pnl=realized,
            total_pnl=unrealized + realized,
            cost_reduction=max(0.0, initial - pos.avg_price) if initial > 0 else 0.0,
            cost_curve=[e.to_dict() for e in events[-50:]],
            breakeven_price=breakeven,
            sell_to_breakeven_qty=sell_to_be,
        )

    def target_position(
        self,
        symbol: str,
        equity: float,
        max_position_pct: float,
        regime: str = "SIDEWAY",
    ) -> float:
        """目标持仓量(数量)

        regime 调整: BULL 满配 / SIDEWAY 半配 / BEAR 降至 1/4 / PANIC 0
        """
        target_quote = equity * max_position_pct
        factor = {"BULL": 1.0, "NORMAL": 0.8, "SIDEWAY": 0.5, "VOLATILE": 0.35, "BEAR": 0.25, "PANIC": 0.0}.get(regime, 0.5)
        target_quote *= factor
        view = self.view(symbol, self.positions.get(symbol).avg_price or 0.0)
        if view.market_price > 0:
            return target_quote / view.market_price
        return 0.0

    def rebalance_suggestion(self, symbol: str, market_price: float, target_qty: float) -> dict[str, Any]:
        """再平衡建议"""
        current = self.positions.get(symbol).quantity
        diff = target_qty - current
        action = "HOLD"
        if diff > 0.01 and market_price > 0:
            action = "BUY"
        elif diff < -0.01:
            action = "SELL"
        return {
            "symbol": symbol,
            "current_qty": round(current, 6),
            "target_qty": round(target_qty, 6),
            "diff_qty": round(diff, 6),
            "action": action,
            "quote": round(abs(diff) * market_price, 2) if market_price > 0 else 0.0,
        }

    def _record(self, symbol: str, event: CostEvent) -> None:
        self.cost_events.setdefault(symbol, []).append(event)
        # 保留最近 200 条
        if len(self.cost_events[symbol]) > 200:
            self.cost_events[symbol] = self.cost_events[symbol][-200:]
