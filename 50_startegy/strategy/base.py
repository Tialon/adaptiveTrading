"""
策略基类与信号定义
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from analytics.engine import MarketAnalytics
from common.utils.logger import LoggerMixin


class SignalSide(str, Enum):
    """信号方向"""

    BUY = "BUY"
    SELL = "SELL"


@dataclass
class Signal:
    """交易信号"""

    symbol: str
    strategy: str
    side: SignalSide
    price: float
    quantity: Optional[float] = None  # 数量(可选,可由风控决定)
    quote_amount: Optional[float] = None  # 目标金额(USDT)
    reason: str = ""
    score: float = 0.0  # 信号强度 0~1

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "strategy": self.strategy,
            "side": self.side.value,
            "price": self.price,
            "quantity": self.quantity,
            "quote_amount": self.quote_amount,
            "reason": self.reason,
            "score": self.score,
        }


class BaseStrategy(LoggerMixin):
    """策略基类

    子类实现 on_market(analytics) -> list[Signal]
    """

    name: str = "base"
    enabled: bool = True

    def __init__(self, symbols: Optional[list[str]] = None):
        from common.config.settings import get_settings

        settings = get_settings()
        self.symbols = symbols or settings.symbol_list
        self._last_signal_ts: dict[str, float] = {}
        self.signal_cooldown: float = 5.0  # 同标的信号冷却(秒)

    def on_market(self, analytics: MarketAnalytics) -> list[Signal]:
        """收到分析快照,返回信号列表"""
        raise NotImplementedError

    def _cooldown_ok(self, symbol: str) -> bool:
        """信号冷却检查"""
        import time

        now = time.time()
        last = self._last_signal_ts.get(symbol, 0.0)
        if now - last < self.signal_cooldown:
            return False
        return True

    def _mark_signal(self, symbol: str) -> None:
        """记录信号时间"""
        import time

        self._last_signal_ts[symbol] = time.time()

    def on_fill(self, signal: Signal, fill_price: float, fill_qty: float) -> None:
        """订单成交回调(子类可选实现)"""
        return

    def status(self) -> dict[str, Any]:
        """策略状态"""
        return {"name": self.name, "enabled": self.enabled, "symbols": self.symbols}
