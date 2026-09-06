"""
纸面交易 Broker(模拟成交)

- 限价单:价格触及即成交(简化:立即按当前价±滑点成交)
- 市价单:立即成交
- 维护模拟现金与手续费
"""

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from at01_common.logger import LoggerMixin


@dataclass
class PaperOrder:
    """模拟订单"""

    client_order_id: str
    symbol: str
    side: str
    order_type: str
    price: Optional[float]
    quantity: float
    status: str = "NEW"
    filled_quantity: float = 0.0
    avg_fill_price: float = 0.0
    fee_paid: float = 0.0
    created_at: float = field(default_factory=time.time)


class PaperBroker(LoggerMixin):
    """纸面交易模拟器"""

    def __init__(self, initial_cash: float = 100000.0, fee_rate: float = 0.001):
        self.cash = initial_cash
        self.initial_cash = initial_cash
        self.fee_rate = fee_rate
        self.orders: dict[str, PaperOrder] = {}

    # ---------- 下单 ----------

    async def create_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float,
        price: Optional[float],
        last_price: float,
    ) -> PaperOrder:
        """创建并立即尝试成交"""
        order = PaperOrder(
            client_order_id=f"paper-{uuid.uuid4().hex[:16]}",
            symbol=symbol,
            side=side.upper(),
            order_type=order_type.upper(),
            price=price,
            quantity=quantity,
        )
        self.orders[order.client_order_id] = order

        fill_price = price if (order.order_type == "LIMIT" and price) else last_price
        # 模拟滑点:买入略高、卖出略低
        slip = fill_price * 0.0002
        if order.side == "BUY":
            fill_price = fill_price + slip
        else:
            fill_price = max(fill_price - slip, 0.0)

        quote = fill_price * quantity
        fee = quote * self.fee_rate

        if order.side == "BUY":
            if quote + fee > self.cash:
                order.status = "REJECTED"
                self.logger.warning(
                    "纸面买入被拒(资金不足)",
                    symbol=symbol, need=round(quote + fee, 2), cash=round(self.cash, 2),
                )
                return order
            self.cash -= quote + fee
        else:
            # 卖出增加现金(手续费从所得扣除)
            self.cash += quote - fee

        order.status = "FILLED"
        order.filled_quantity = quantity
        order.avg_fill_price = fill_price
        order.fee_paid = fee
        self.logger.info(
            "纸面成交", symbol=symbol, side=order.side,
            qty=quantity, price=round(fill_price, 2), fee=round(fee, 2),
            cash=round(self.cash, 2),
        )
        return order

    async def cancel_order(self, client_order_id: str) -> bool:
        """撤单"""
        order = self.orders.get(client_order_id)
        if order and order.status == "NEW":
            order.status = "CANCELED"
            return True
        return False

    def get_order(self, client_order_id: str) -> Optional[PaperOrder]:
        return self.orders.get(client_order_id)

    def status(self) -> dict[str, Any]:
        return {
            "cash": round(self.cash, 2),
            "initial_cash": self.initial_cash,
            "pnl": round(self.cash - self.initial_cash, 2),
            "order_count": len(self.orders),
        }
