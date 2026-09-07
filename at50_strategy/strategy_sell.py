"""
Exit Strategy(V2.0)

三种退出方式(可叠加,输出各自原因与分批比例):
1. 分批止盈: 盈利 5% 卖 20% / 10% 卖 30% / 20% 卖 50%
2. 移动止盈: 价格自峰值回撤超阈值(默认 5%)卖出
3. 趋势退出: EMA 死叉 + CVD 下降 + 主动买减少 -> 清仓
"""

from typing import Callable, Optional

from at30_analytics.engine import MarketAnalytics
from at01_common.settings import get_settings
from at50_strategy.strategy_base import BaseStrategy, Signal, SignalSide

# 查询持仓: symbol -> (trade_quantity, avg_price, peak_price)  V5: 只暴露交易仓
PositionProvider = Callable[[str], Optional[tuple[float, float, float]]]


def _parse_ladder(raw: str) -> list[tuple[float, float]]:
    """解析分批止盈阶梯 "5:20,10:30,20:50" -> [(0.05, 0.20), ...]"""
    ladder: list[tuple[float, float]] = []
    for part in raw.split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        left, right = part.split(":", 1)
        try:
            pct = float(left) / 100.0
            ratio = float(right) / 100.0
        except ValueError:
            continue
        if pct > 0 and 0 < ratio <= 1.0:
            ladder.append((pct, ratio))
    # 按盈利比例升序(从小到大依次命中)
    return sorted(ladder, key=lambda x: x[0])


class SellStrategy(BaseStrategy):
    """卖出策略(Exit)"""

    name = "exit"

    # 分批止盈阶梯默认值(盈利比例, 卖出持仓比例); 实盘由 settings.sell_take_profit_ladder 覆盖
    TAKE_PROFIT_LADDER: list[tuple[float, float]] = [
        (0.05, 0.20),
        (0.10, 0.30),
        (0.20, 0.50),
    ]

    # V3.0: Exit Reason 结构化标签(供复盘/优化)
    # profit_target: 止盈 | overbought: 超买回落(移动止盈) |
    # trend_reverse: 趋势反转 | risk_reduce: 降风险/降成本
    EXIT_TAG_PROFIT_TARGET = "profit_target"
    EXIT_TAG_OVERBOUGHT = "overbought"
    EXIT_TAG_TREND_REVERSE = "trend_reverse"
    EXIT_TAG_RISK_REDUCE = "risk_reduce"

    def __init__(self, symbols: Optional[list[str]] = None):
        super().__init__(symbols)
        settings = get_settings()
        self.trailing_drawdown = settings.sell_trailing_drawdown
        # V9.0: 止盈阶梯 settings 化(解析失败回退默认)
        ladder = _parse_ladder(settings.sell_take_profit_ladder)
        self.take_profit_ladder = ladder if ladder else list(self.TAKE_PROFIT_LADDER)

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
        # V5: 交易策略只操作交易仓(核心仓由 Allocation 引导, 此处不看)

        profit_ratio = (a.price - avg_price) / avg_price if avg_price > 0 else 0.0
        peak = max(peak_price, a.price)

        reasons: list[str] = []
        sell_ratio = 0.0  # 本次卖出持仓比例(取最大者)
        score = 0.0

        # ---- 1. 分批止盈 ----
        ladder_hit = None
        for profit_threshold, ratio in self.take_profit_ladder:
            if profit_ratio >= profit_threshold:
                ladder_hit = (profit_threshold, ratio)
        exit_tags = []
        if ladder_hit is not None:
            reasons.append(f"[{self.EXIT_TAG_PROFIT_TARGET}] 分批止盈: 盈利{profit_ratio:.1%} ≥ {ladder_hit[0]:.0%}, 卖{ladder_hit[1]:.0%}")
            exit_tags.append(self.EXIT_TAG_PROFIT_TARGET)
            sell_ratio = max(sell_ratio, ladder_hit[1])
            score += 0.6

        # ---- 2. 移动止盈 ----
        if peak > 0 and a.price < peak:
            drawdown = (peak - a.price) / peak
            if drawdown >= self.trailing_drawdown and profit_ratio > 0:
                reasons.append(f"[{self.EXIT_TAG_OVERBOUGHT}] 移动止盈: 峰值{peak:.2f}回撤{drawdown:.1%} ≥ {self.trailing_drawdown:.0%}")
                exit_tags.append(self.EXIT_TAG_OVERBOUGHT)
                sell_ratio = 1.0
                score += 0.8

        # ---- 3. 趋势退出(三条件中二) ----
        trend_conditions = [
            a.trend == "down",
            a.cvd_falling,
            a.delta_ratio < 0,
        ]
        if sum(trend_conditions) >= 2 and profit_ratio > -0.005:
            reasons.append(
                f"[{self.EXIT_TAG_TREND_REVERSE}] 趋势退出: EMA{'死叉' if trend_conditions[0] else '·'} "
                f"CVD{'降' if trend_conditions[1] else '·'} 买压{'减' if trend_conditions[2] else '·'}"
            )
            exit_tags.append(self.EXIT_TAG_TREND_REVERSE)
            sell_ratio = 1.0
            score += 0.7
        # 熊市降风险: 亏损中的趋势退出视为降仓
        if sum(trend_conditions) >= 2 and profit_ratio <= 0 and exit_tags:
            exit_tags.append(self.EXIT_TAG_RISK_REDUCE)

        if not reasons or sell_ratio <= 0:
            return []

        sell_qty = quantity * sell_ratio
        # 最小名义价值过滤(交给风控二次校验)
        if sell_qty * a.price < 10:
            return []

        self._mark_signal(a.symbol)
        return [
            Signal(
                symbol=a.symbol,
                strategy=self.name,
                side=SignalSide.SELL,
                price=a.price,
                quantity=sell_qty,
                bucket="trade",  # V5: 只卖交易仓
                reason=reasons,
                score=round(min(100.0, score * 100), 1),
                indicators={
                    "profit_ratio": round(profit_ratio, 4),
                    "peak_price": peak,
                    "drawdown_from_peak": round((peak - a.price) / peak, 4) if peak > 0 else 0.0,
                    "trend": a.trend,
                    "cvd_falling": a.cvd_falling,
                    "delta_ratio": round(a.delta_ratio, 4),
                    "sell_ratio": sell_ratio,
                    "exit_tags": exit_tags,
                    "position_quantity": quantity,
                    "avg_price": avg_price,
                },
            )
        ]
