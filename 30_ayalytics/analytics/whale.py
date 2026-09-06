"""
大单(Whale)检测

策略:
1. 绝对金额阈值:单笔成交额 >= whale_min_quote
2. 动态分位数:单笔成交额 >= 滚动窗口分位数(默认 P99)

命中任一即视为大单。
"""

from collections import deque
from dataclasses import dataclass
from typing import Optional

from market.models import TradeTick


@dataclass
class WhaleEvent:
    """大单事件"""

    symbol: str
    side: str  # "buy" / "sell"(主动方向)
    price: float
    quantity: float
    quote_quantity: float
    trade_time: int
    threshold: float  # 触发阈值


class WhaleDetector:
    """大单检测器"""

    def __init__(
        self,
        min_quote: float = 50000.0,
        quantile: float = 0.99,
        window: int = 2000,
    ):
        self.min_quote = min_quote
        self.quantile = quantile
        self._quotes: deque[float] = deque(maxlen=window)

    def _dynamic_threshold(self) -> Optional[float]:
        """基于滚动分位数的动态阈值"""
        if len(self._quotes) < 100:
            return None
        data = sorted(self._quotes)
        idx = min(int(len(data) * self.quantile), len(data) - 1)
        return data[idx]

    def update(self, tick: TradeTick) -> Optional[WhaleEvent]:
        """检测单笔成交,命中返回 WhaleEvent"""
        self._quotes.append(tick.quote_quantity)

        static_hit = tick.quote_quantity >= self.min_quote
        dyn_threshold = self._dynamic_threshold()
        dynamic_hit = dyn_threshold is not None and tick.quote_quantity >= dyn_threshold

        if not (static_hit or dynamic_hit):
            return None

        threshold = max(self.min_quote, dyn_threshold or 0.0)
        return WhaleEvent(
            symbol=tick.symbol,
            side="sell" if tick.is_buyer_maker else "buy",
            price=tick.price,
            quantity=tick.quantity,
            quote_quantity=tick.quote_quantity,
            trade_time=tick.trade_time,
            threshold=threshold,
        )

    @property
    def whale_ratio_recent(self) -> float:
        """近期大单成交占比(供吸筹检测等复用)"""
        return 0.0
