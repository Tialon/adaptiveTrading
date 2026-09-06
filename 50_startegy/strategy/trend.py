"""
趋势策略:EMA 快慢线交叉 + CVD 确认

- 金叉(快线上穿慢线)且 CVD 上升 -> 买入
- 死叉(快线下穿慢线)且持仓 -> 卖出
"""

from typing import Optional

from analytics.engine import MarketAnalytics
from common.config.settings import get_settings
from strategy.base import BaseStrategy, Signal, SignalSide


class TrendStrategy(BaseStrategy):
    """趋势跟随策略"""

    name = "trend"

    def __init__(self, symbols: Optional[list[str]] = None):
        super().__init__(symbols)
        settings = get_settings()
        self.fast_period = settings.trend_fast_period
        self.slow_period = settings.trend_slow_period
        self.quote_amount = settings.risk_max_single_order_quote * 0.8
        self._last_trend: dict[str, str] = {}

    def on_market(self, a: MarketAnalytics) -> list[Signal]:
        if a.symbol not in self.symbols or a.price <= 0:
            return []
        if a.ema_fast <= 0 or a.ema_slow <= 0:
            return []
        if not self._cooldown_ok(a.symbol):
            return []

        prev = self._last_trend.get(a.symbol, "neutral")
        current = a.trend
        self._last_trend[a.symbol] = current

        signals: list[Signal] = []

        # 金叉:neutral -> up,且 CVD 确认
        if prev != "up" and current == "up" and (a.cvd_rising or a.delta_ratio > 0):
            self._mark_signal(a.symbol)
            signals.append(
                Signal(
                    symbol=a.symbol,
                    strategy=self.name,
                    side=SignalSide.BUY,
                    price=a.price,
                    quote_amount=self.quote_amount,
                    reason=(
                        f"金叉 EMA{self.fast_period}>{self.slow_period} "
                        f"CVD{'↑' if a.cvd_rising else '·'} Δ{a.delta_ratio:.1%}"
                    ),
                    score=0.7,
                )
            )

        # 死叉
        if prev != "down" and current == "down" and (a.cvd_falling or a.delta_ratio < 0):
            self._mark_signal(a.symbol)
            signals.append(
                Signal(
                    symbol=a.symbol,
                    strategy=self.name,
                    side=SignalSide.SELL,
                    price=a.price,
                    quote_amount=self.quote_amount,
                    reason=f"死叉 EMA{self.fast_period}<{self.slow_period}",
                    score=0.7,
                )
            )

        return signals

    def status(self) -> dict:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "fast": self.fast_period,
            "slow": self.slow_period,
            "last_trend": dict(self._last_trend),
        }
