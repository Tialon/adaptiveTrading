"""
卖出策略:止盈 + 移动止盈 + 趋势反转离场

基于持仓信息(由 StrategyEngine 注入 PositionProvider):
1. 固定止盈:价格 >= 持仓均价 * (1 + profit_pct)
2. 移动止盈:价格自持仓峰值回撤 >= trailing_drawdown
3. 趋势反转:趋势转 down 且 CVD 下行
"""

from typing import Callable, Optional

from analytics.engine import MarketAnalytics
from common.config.settings import get_settings
from strategy.base import BaseStrategy, Signal, SignalSide

# 查询持仓: symbol -> (quantity, avg_price, peak_price)
PositionProvider = Callable[[str], Optional[tuple[float, float, float]]]


class SellStrategy(BaseStrategy):
    """持仓止盈卖出策略"""

    name = "sell"

    def __init__(self, symbols: Optional[list[str]] = None):
        super().__init__(symbols)
        settings = get_settings()
        self.profit_pct = settings.sell_profit_pct
        self.trailing_drawdown = settings.sell_trailing_drawdown
        self.position_provider: Optional[PositionProvider] = None

    def on_market(self, a: MarketAnalytics) -> list[Signal]:
        if a.symbol not in self.symbols or a.price <= 0:
            return []
        if self.position_provider is None:
            return []
        if not self._cooldown_ok(a.symbol):
            return []

        pos = self.position_provider(a.symbol)
        if not pos or pos[0] <= 0:
            return []
        quantity, avg_price, peak_price = pos

        # 更新峰值
        peak = max(peak_price, a.price)

        reasons: list[str] = []
        score = 0.0

        # 1. 固定止盈
        profit_ratio = (a.price - avg_price) / avg_price if avg_price > 0 else 0.0
        if profit_ratio >= self.profit_pct:
            reasons.append(f"止盈{profit_ratio:.2%}")
            score += 0.7

        # 2. 移动止盈(从峰值回撤,且仍有浮盈)
        if peak > 0 and a.price < peak:
            drawdown = (peak - a.price) / peak
            if drawdown >= self.trailing_drawdown and profit_ratio > 0:
                reasons.append(f"峰值回撤{drawdown:.2%}")
                score += 0.7

        # 3. 趋势反转
        if a.trend == "down" and a.cvd_falling and profit_ratio > -0.002:
            reasons.append("趋势反转下行")
            score += 0.5

        if not reasons:
            return []

        self._mark_signal(a.symbol)
        return [
            Signal(
                symbol=a.symbol,
                strategy=self.name,
                side=SignalSide.SELL,
                price=a.price,
                quantity=quantity,  # 全部卖出
                reason="; ".join(reasons),
                score=round(min(1.0, score), 3),
            )
        ]
