"""
行情内存数据结构
"""

from collections import deque
from dataclasses import dataclass, field
from typing import Optional


@dataclass(slots=True)
class TradeTick:
    """逐笔成交(聚合)"""

    trade_id: int
    symbol: str
    price: float
    quantity: float
    quote_quantity: float
    is_buyer_maker: bool  # True=主动卖, False=主动买
    trade_time: int  # ms


@dataclass(slots=True)
class KlineBar:
    """K线"""

    symbol: str
    interval: str
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float
    trade_count: int
    closed: bool = False


@dataclass(slots=True)
class DepthState:
    """盘口状态"""

    bids: list[list[float]] = field(default_factory=list)  # [price, qty]
    asks: list[list[float]] = field(default_factory=list)

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0][0] if self.asks else None

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    @property
    def mid_price(self) -> Optional[float]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2


@dataclass
class SymbolState:
    """单个标的的实时状态"""

    symbol: str
    last_price: float = 0.0
    mark_change_pct_24h: float = 0.0
    high_24h: float = 0.0
    low_24h: float = 0.0
    quote_volume_24h: float = 0.0
    depth: DepthState = field(default_factory=DepthState)
    trades: deque[TradeTick] = field(default_factory=lambda: deque(maxlen=500))
    klines: deque[KlineBar] = field(default_factory=lambda: deque(maxlen=200))
    updated_at: int = 0  # ms
