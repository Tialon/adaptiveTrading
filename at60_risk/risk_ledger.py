"""
Portfolio Ledger(V6.0)

交易账本 —— 金融正确性的基础:

每笔成交记录完整状态转换:
    timestamp / symbol / bucket / side / qty / price / fee /
    cash_before / cash_after /
    position_before / position_after /
    realized_pnl(按各仓独立加权成本计算)

解决:
- core/trade 成本混淆(trade PnL 误用 core_cost)
- 手续费遗漏
- 资产对不上(initial + realized - fees = final)

对账 API:
    ledger.reconcile(initial_cash, final_cash, position, last_price)
"""

from dataclasses import dataclass, field
from typing import Any, Optional

from at01_common.logger import LoggerMixin


@dataclass
class LedgerEntry:
    """账本条目"""

    ts: int  # ms
    symbol: str
    bucket: str  # core / trade
    side: str  # BUY / SELL
    qty: float
    price: float
    fee: float = 0.0
    cash_before: float = 0.0
    cash_after: float = 0.0
    position_before: float = 0.0  # 该 bucket 数量
    position_after: float = 0.0
    avg_cost_before: float = 0.0  # 该 bucket 加权成本
    avg_cost_after: float = 0.0
    realized_pnl: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts, "bucket": self.bucket, "side": self.side,
            "qty": self.qty, "price": self.price, "fee": self.fee,
            "cash_after": round(self.cash_after, 2),
            "position_after": round(self.position_after, 6),
            "avg_cost_after": round(self.avg_cost_after, 4),
            "realized_pnl": round(self.realized_pnl, 2),
        }


@dataclass
class Reconciliation:
    """对账结果"""

    balanced: bool = False
    initial_cash: float = 0.0
    final_cash: float = 0.0
    total_realized: float = 0.0
    total_fees: float = 0.0
    unrealized: float = 0.0
    expected_cash: float = 0.0
    diff: float = 0.0  # final_cash - expected_cash(应=0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "balanced": self.balanced,
            "initial_cash": round(self.initial_cash, 2),
            "final_cash": round(self.final_cash, 2),
            "total_realized": round(self.total_realized, 2),
            "total_fees": round(self.total_fees, 2),
            "unrealized": round(self.unrealized, 2),
            "expected_cash": round(self.expected_cash, 2),
            "diff": round(self.diff, 4),
        }


class PortfolioLedger(LoggerMixin):
    """双仓交易账本(内存, 可序列化)"""

    def __init__(self):
        self.entries: list[LedgerEntry] = []
        # 各 symbol 各 bucket 状态
        self._qty: dict[str, dict[str, float]] = {}      # {sym: {core: q, trade: q}}
        self._cost: dict[str, dict[str, float]] = {}     # 加权成本
        self._cash: float = 0.0
        self._realized: dict[str, float] = {}            # {sym: realized}
        self._fees: dict[str, float] = {}                # {sym: fees}

    def init_cash(self, cash: float) -> None:
        self._cash = cash

    # ---------- 成交记账(单一入口, 双仓独立成本) ----------

    def record_fill(
        self,
        ts: int,
        symbol: str,
        bucket: str,
        side: str,
        qty: float,
        price: float,
        fee: float,
    ) -> LedgerEntry:
        """记录一笔成交, 返回账本条目(含精确 realized_pnl)"""
        qty_before = self._qty.setdefault(symbol, {"core": 0.0, "trade": 0.0}).get(bucket, 0.0)
        cost_before = self._cost.setdefault(symbol, {"core": 0.0, "trade": 0.0}).get(bucket, 0.0)
        cash_before = self._cash
        realized = 0.0

        if side.upper() == "BUY":
            # 加权成本(费用计入成本)
            new_qty = qty_before + qty
            cost_after = (qty_before * cost_before + qty * price + fee) / new_qty if new_qty > 0 else 0.0
            self._cash = cash_before - qty * price - fee
        else:
            # 卖出: 按该 bucket 成本计算 realized(费用扣除)
            realized = (price - cost_before) * qty - fee
            new_qty = max(0.0, qty_before - qty)
            cost_after = cost_before if new_qty > 0 else 0.0
            self._cash = cash_before + qty * price - fee
            self._realized[symbol] = self._realized.get(symbol, 0.0) + realized

        self._fees[symbol] = self._fees.get(symbol, 0.0) + fee
        self._qty[symbol][bucket] = new_qty
        self._cost[symbol][bucket] = cost_after

        entry = LedgerEntry(
            ts=ts, symbol=symbol, bucket=bucket, side=side.upper(),
            qty=qty, price=price, fee=fee,
            cash_before=cash_before, cash_after=self._cash,
            position_before=qty_before, position_after=new_qty,
            avg_cost_before=cost_before, avg_cost_after=cost_after,
            realized_pnl=realized,
        )
        self.entries.append(entry)
        return entry

    # ---------- 查询 ----------

    def cash(self) -> float:
        return self._cash

    def qty(self, symbol: str, bucket: str) -> float:
        return self._qty.get(symbol, {}).get(bucket, 0.0)

    def avg_cost(self, symbol: str, bucket: str) -> float:
        return self._cost.get(symbol, {}).get(bucket, 0.0)

    def realized(self, symbol: str) -> float:
        return self._realized.get(symbol, 0.0)

    def fees(self, symbol: str) -> float:
        return self._fees.get(symbol, 0.0)

    # ---------- 对账(P0-9 核心不变量) ----------

    def reconcile(
        self,
        symbol: str,
        final_cash: float,
        last_price: float,
        initial_cash: float,
        tolerance: float = 0.01,
    ) -> Reconciliation:
        """对账: expected_cash = initial + 已实现 - (持仓成本) 逐笔推演

        更直接: expected_cash = final_cash(账本自身现金),
        外部 final_cash(如 broker)应与账本一致。
        """
        unrealized = 0.0
        for bucket in ("core", "trade"):
            q = self.qty(symbol, bucket)
            c = self.avg_cost(symbol, bucket)
            unrealized += (last_price - c) * q

        # 账本自身推演: initial - 买入流出 + 卖出流入 = 当前现金
        # (每笔已含费, 精确对账)
        r = Reconciliation(
            initial_cash=initial_cash,
            final_cash=final_cash,
            total_realized=self.realized(symbol),
            total_fees=self.fees(symbol),
            unrealized=unrealized,
            expected_cash=self._cash,
            diff=final_cash - self._cash,
        )
        r.balanced = abs(r.diff) <= tolerance
        if not r.balanced:
            self.logger.error("账本对账失败", **r.to_dict())
        return r

    # ---------- 导出 ----------

    def to_dicts(self, limit: int = 100) -> list[dict[str, Any]]:
        return [e.to_dict() for e in self.entries[-limit:]]
