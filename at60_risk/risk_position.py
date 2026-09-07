"""
持仓管理

内存维护 + 数据库镜像(启动时加载,变更时持久化)。
"""

from dataclasses import dataclass
from typing import Any, Optional

from at01_common.logger import LoggerMixin


@dataclass
class PositionState:
    """内存持仓状态"""

    symbol: str
    quantity: float = 0.0
    avg_price: float = 0.0
    realized_pnl: float = 0.0
    peak_price: float = 0.0
    # V9.0: 成交闭环跟踪(内存态, 供 TradingJournal 计算持仓期/最大浮盈/回撤)
    entry_ts: float = 0.0  # 首次建仓时间(epoch 秒)
    trough_price: float = 0.0  # 持仓期间最低价(最大回撤)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "quantity": self.quantity,
            "avg_price": self.avg_price,
            "realized_pnl": self.realized_pnl,
            "peak_price": self.peak_price,
        }


class PositionManager(LoggerMixin):
    """持仓管理器(现货多头)"""

    def __init__(self):
        self.positions: dict[str, PositionState] = {}

    # ---------- 查询 ----------

    def get(self, symbol: str) -> PositionState:
        """获取持仓(不存在则创建空仓)"""
        if symbol not in self.positions:
            self.positions[symbol] = PositionState(symbol=symbol)
        return self.positions[symbol]

    def get_or_none(self, symbol: str) -> Optional[PositionState]:
        pos = self.positions.get(symbol)
        if pos and pos.quantity <= 0:
            return None
        return pos

    def total_position_quote(self, last_prices: dict[str, float]) -> float:
        """总持仓市值"""
        total = 0.0
        for pos in self.positions.values():
            if pos.quantity > 0:
                price = last_prices.get(pos.symbol, pos.avg_price)
                total += pos.quantity * price
        return total

    # ---------- 变更 ----------

    def apply_buy(self, symbol: str, quantity: float, price: float, fee_quote: float = 0.0) -> PositionState:
        """买入成交"""
        pos = self.get(symbol)
        # V9.0: 首次建仓记录起始时间与最低价基准
        if pos.quantity <= 0:
            import time

            pos.entry_ts = time.time()
            pos.trough_price = price
        total_cost = pos.quantity * pos.avg_price + quantity * price + fee_quote
        pos.quantity += quantity
        pos.avg_price = total_cost / pos.quantity if pos.quantity > 0 else 0.0
        pos.peak_price = max(pos.peak_price, price)
        self.logger.info(
            "买入成交", symbol=symbol, qty=quantity, price=price,
            position=pos.quantity, avg_price=round(pos.avg_price, 2),
        )
        return pos

    def apply_sell(self, symbol: str, quantity: float, price: float, fee_quote: float = 0.0) -> tuple[PositionState, float]:
        """卖出成交,返回 (持仓, 已实现盈亏)"""
        pos = self.get(symbol)
        qty = min(quantity, pos.quantity)
        if qty > 0 and pos.avg_price > 0:
            pnl = (price - pos.avg_price) * qty - fee_quote
            pos.realized_pnl += pnl
        else:
            pnl = -fee_quote
        pos.quantity = max(0.0, pos.quantity - qty)
        if pos.quantity <= 0:
            pos.avg_price = 0.0
            pos.peak_price = 0.0
            pos.entry_ts = 0.0
            pos.trough_price = 0.0
        self.logger.info(
            "卖出成交", symbol=symbol, qty=qty, price=price,
            pnl=round(pnl, 2), remaining=pos.quantity,
        )
        return pos, pnl

    def update_price(self, symbol: str, price: float) -> None:
        """更新峰值/谷值价格(移动止盈 + 最大回撤跟踪)"""
        pos = self.positions.get(symbol)
        if pos and pos.quantity > 0:
            if price > pos.peak_price:
                pos.peak_price = price
            if pos.trough_price <= 0 or price < pos.trough_price:
                pos.trough_price = price

    def unrealized_pnl(self, symbol: str, last_price: float) -> float:
        """未实现盈亏"""
        pos = self.positions.get(symbol)
        if not pos or pos.quantity <= 0:
            return 0.0
        return (last_price - pos.avg_price) * pos.quantity

    # ---------- V2.0: 持仓管理查询 ----------

    def sellable_quantity(self, symbol: str, last_price: float = 0.0) -> float:
        """可卖数量(全部持仓,现货无 T+1)"""
        pos = self.positions.get(symbol)
        return pos.quantity if pos and pos.quantity > 0 else 0.0

    def buyable_quote(
        self, symbol: str, available_cash: float, max_position_quote: float, last_price: float
    ) -> float:
        """可买额度(USDT): min(可用现金, 仓位限额剩余)"""
        pos = self.positions.get(symbol)
        current_quote = pos.quantity * last_price if pos else 0.0
        room = max(0.0, max_position_quote - current_quote)
        return min(available_cash, room)

    def position_report(self, symbol: str, last_price: float) -> dict[str, Any]:
        """持仓报告(Position Engine 核心输出)"""
        pos = self.positions.get(symbol)
        unrealized = self.unrealized_pnl(symbol, last_price)
        return {
            "symbol": symbol,
            "quantity": pos.quantity,
            "avg_cost": pos.avg_price,
            "market_price": last_price,
            "unrealized_profit": round(unrealized, 2),
            "realized_profit": round(pos.realized_pnl, 2),
            "total_profit": round(unrealized + pos.realized_pnl, 2),
            "cost_basis": round(pos.quantity * pos.avg_price, 2),
            "market_value": round(pos.quantity * last_price, 2),
            "peak_price": pos.peak_price,
            "profit_ratio": round(
                (last_price - pos.avg_price) / pos.avg_price, 4
            ) if pos.avg_price > 0 else 0.0,
        }

    async def snapshot_to_db(self, symbol: str, last_price: float, equity: float = 0.0) -> None:
        """定时持仓快照落库(position_snapshot 表)"""
        from at01_common.database import AsyncSessionLocal
        from at01_common.models import PositionSnapshot

        pos = self.positions.get(symbol)
        if pos is None:
            return  # V7fix: 未建仓不快照
        unrealized = self.unrealized_pnl(symbol, last_price)
        try:
            async with AsyncSessionLocal() as session:
                session.add(
                    PositionSnapshot(
                        symbol=symbol,
                        quantity=pos.quantity,
                        avg_cost=pos.avg_price,
                        market_price=last_price,
                        unrealized_profit=unrealized,
                        realized_profit=pos.realized_pnl,
                        equity=equity,
                    )
                )
                await session.commit()
        except Exception:
            self.logger.exception("持仓快照落库失败", symbol=symbol)

    # ---------- 持久化 ----------

    async def load_from_db(self) -> None:
        """启动时从数据库加载持仓"""
        from sqlalchemy import select

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import Position

        try:
            async with AsyncSessionLocal() as session:
                rows = (await session.execute(select(Position))).scalars().all()
                for row in rows:
                    self.positions[row.symbol] = PositionState(
                        symbol=row.symbol,
                        quantity=row.quantity,
                        avg_price=row.avg_price,
                        realized_pnl=row.realized_pnl,
                        peak_price=row.peak_price,
                    )
            self.logger.info("持仓已加载", count=len(self.positions))
        except Exception:
            self.logger.exception("持仓加载失败")

    async def persist(self, symbol: str) -> None:
        """持久化单标的持仓(不存在则创建)"""
        from sqlalchemy import select

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import Position

        pos = self.positions.get(symbol)
        if pos is None:
            return
        try:
            async with AsyncSessionLocal() as session:
                row = (
                    await session.execute(
                        select(Position).where(Position.symbol == symbol)
                    )
                ).scalar_one_or_none()
                if row is None:
                    row = Position(symbol=symbol)
                    session.add(row)
                row.quantity = pos.quantity
                row.avg_price = pos.avg_price
                row.realized_pnl = pos.realized_pnl
                row.peak_price = pos.peak_price
                await session.commit()
        except Exception:
            self.logger.exception("持仓持久化失败", symbol=symbol)
