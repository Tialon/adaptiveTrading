"""
持仓管理

内存维护 + 数据库镜像(启动时加载,变更时持久化)。
"""

from dataclasses import dataclass
from typing import Any, Optional

from common.utils.logger import LoggerMixin


@dataclass
class PositionState:
    """内存持仓状态"""

    symbol: str
    quantity: float = 0.0
    avg_price: float = 0.0
    realized_pnl: float = 0.0
    peak_price: float = 0.0

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
        self.logger.info(
            "卖出成交", symbol=symbol, qty=qty, price=price,
            pnl=round(pnl, 2), remaining=pos.quantity,
        )
        return pos, pnl

    def update_price(self, symbol: str, price: float) -> None:
        """更新峰值价格(移动止盈用)"""
        pos = self.positions.get(symbol)
        if pos and pos.quantity > 0 and price > pos.peak_price:
            pos.peak_price = price

    def unrealized_pnl(self, symbol: str, last_price: float) -> float:
        """未实现盈亏"""
        pos = self.positions.get(symbol)
        if not pos or pos.quantity <= 0:
            return 0.0
        return (last_price - pos.avg_price) * pos.quantity

    # ---------- 持久化 ----------

    async def load_from_db(self) -> None:
        """启动时从数据库加载持仓"""
        from sqlalchemy import select

        from common.config.database import AsyncSessionLocal
        from common.models import Position

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

        from common.config.database import AsyncSessionLocal
        from common.models import Position

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
