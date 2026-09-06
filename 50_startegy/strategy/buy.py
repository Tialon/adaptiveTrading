"""
买入策略:吸筹 + 折价买入

条件(可叠加评分):
- 价格低于 VWAP 达到折价阈值(超卖)
- 吸筹分数高且 CVD 上升(主力吸筹)
- 趋势非 down(不逆势接刀)

满足 2 条以上发出买入信号。
"""

from typing import Optional

from analytics.engine import MarketAnalytics
from common.config.settings import get_settings
from strategy.base import BaseStrategy, Signal, SignalSide


class BuyStrategy(BaseStrategy):
    """吸筹折价买入策略"""

    name = "buy"

    def __init__(self, symbols: Optional[list[str]] = None):
        super().__init__(symbols)
        settings = get_settings()
        self.dip_pct = settings.buy_dip_pct
        self.quote_amount = settings.risk_max_single_order_quote * 0.5

    def on_market(self, a: MarketAnalytics) -> list[Signal]:
        if a.symbol not in self.symbols or a.price <= 0:
            return []
        if not self._cooldown_ok(a.symbol):
            return []

        conditions: list[tuple[bool, str, float]] = []

        # 1. 价格低于 VWAP 折价
        dip = -a.vwap_deviation  # 正值=折价
        conditions.append(
            (dip >= self.dip_pct, f"VWAP折价{dip:.3%}", min(1.0, dip / (self.dip_pct * 3)))
        )

        # 2. 吸筹信号
        conditions.append(
            (a.is_accumulating, f"吸筹分数{a.accumulation:.2f}", a.accumulation)
        )

        # 3. CVD 上升(买方主导)
        conditions.append((a.cvd_rising, "CVD上升", 0.5 if a.cvd_rising else 0.0))

        # 4. 趋势非向下
        conditions.append((a.trend != "down", "趋势非down", 0.3))

        passed = [(ok, why, sc) for ok, why, sc in conditions if ok]
        if len(passed) < 2:
            return []

        score = sum(sc for _, _, sc in passed) / len(conditions)
        reason = "; ".join(why for _, why, _ in passed)

        self._mark_signal(a.symbol)
        return [
            Signal(
                symbol=a.symbol,
                strategy=self.name,
                side=SignalSide.BUY,
                price=a.price,
                quote_amount=self.quote_amount,
                reason=reason,
                score=round(score, 3),
            )
        ]
