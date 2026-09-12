"""
Alpha Engine(V3.0)

市场机会综合评分(与单策略评分解耦, 策略"如何执行", Alpha 决定"值不值得做"):

    价格因子   30%   (VWAP偏离 + 区间位置)
    资金流     25%   (CVD斜率 + delta_ratio + 大单偏向)
    趋势       20%   (EMA结构 + SOL/BTC相对强弱)
    波动率     15%   (振幅适中得高分, 过低/过高降分)
    情绪       10%   (24h涨跌幅 + 量比)

输出 Alpha Score 0~100 + 各因子明细(可解释)。
"""

from dataclasses import dataclass, field
from typing import Any

from at20_analytics.engine import MarketAnalytics


@dataclass
class AlphaScore:
    """Alpha 综合评分"""

    symbol: str
    score: float = 0.0  # 0~100
    price_factor: float = 0.0  # 各因子 0~1
    flow_factor: float = 0.0
    trend_factor: float = 0.0
    volatility_factor: float = 0.0
    sentiment_factor: float = 0.0
    details: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "score": round(self.score, 1),
            "factors": {
                "price": round(self.price_factor, 3),
                "flow": round(self.flow_factor, 3),
                "trend": round(self.trend_factor, 3),
                "volatility": round(self.volatility_factor, 3),
                "sentiment": round(self.sentiment_factor, 3),
            },
            "details": self.details,
        }


class AlphaEngine:
    """市场机会综合评分引擎"""

    WEIGHTS = {
        "price": 0.30,
        "flow": 0.25,
        "trend": 0.20,
        "volatility": 0.15,
        "sentiment": 0.10,
    }

    def score(self, a: MarketAnalytics, btc_change_24h: float = 0.0) -> AlphaScore:
        """综合评分"""
        details: list[str] = []

        # ---- 价格因子 30% ----
        # VWAP 折价 + 区间低位 -> 高分(逢低)
        dip = max(0.0, -a.vwap_deviation) if a.vwap > 0 else 0.0
        vwap_part = min(1.0, dip / 0.02)
        pos_part = 0.5
        if a.recent_high > a.recent_low > 0:
            pos_part = max(0.0, min(1.0, (a.recent_high - a.price) / (a.recent_high - a.recent_low)))
        price_factor = vwap_part * 0.6 + pos_part * 0.4
        details.append(f"价格: VWAP折价{dip:.2%} 区间位置{pos_part:.0%}")

        # ---- 资金流 25% ----
        cvd_part = min(1.0, max(0.0, a.cvd_slope / 0.5)) if a.cvd_rising else 0.0
        delta_part = min(1.0, max(0.0, a.delta_ratio / 0.2))
        whale_part = 0.5
        total_whales = a.whale_buy_count_recent + a.whale_sell_count_recent
        if total_whales > 0:
            whale_part = a.whale_buy_count_recent / total_whales
        flow_factor = cvd_part * 0.4 + delta_part * 0.4 + whale_part * 0.2
        details.append(
            f"资金流: CVD{'↑' if a.cvd_rising else '↓'} 买压{a.delta_ratio:+.1%} 大单买比{whale_part:.0%}"
        )

        # ---- 趋势 20% ----
        trend_part = {"up": 1.0, "neutral": 0.5, "down": 0.0}.get(a.trend, 0.5)
        # BTC 相对强弱: 标的 24h 涨幅 - BTC 24h 涨幅, 跑赢加分
        rel = max(-1.0, min(1.0, (a.change_pct_24h - btc_change_24h) / 5.0))
        rel_part = 0.5 + rel * 0.5
        trend_factor = trend_part * 0.8 + rel_part * 0.2
        details.append(f"趋势: {a.trend} BTC24h{btc_change_24h:+.1f}%")

        # ---- 波动率 15% ----
        # 适中波动(0.3%~1.5%)最佳: 有交易空间且不过度危险
        vol = 0.0
        if a.recent_high > 0 and a.recent_low > 0:
            vol = (a.recent_high - a.recent_low) / ((a.recent_high + a.recent_low) / 2)
        if 0.003 <= vol <= 0.015:
            volatility_factor = 1.0
        elif vol < 0.003:
            volatility_factor = 0.4  # 过低: 无肉
        elif vol <= 0.03:
            volatility_factor = 0.6  # 偏高
        else:
            volatility_factor = 0.2  # 恐慌
        details.append(f"波动率: 振幅{vol:.2%}")

        # ---- 情绪 10% ----
        # 温和上涨最优, 暴涨暴跌降分
        chg = a.change_pct_24h if hasattr(a, "change_pct_24h") else 0.0
        if 0 < chg <= 3:
            sentiment_factor = 1.0
        elif -2 <= chg <= 0:
            sentiment_factor = 0.7
        elif 3 < chg <= 7:
            sentiment_factor = 0.6  # 追高风险
        else:
            sentiment_factor = 0.3  # 暴涨暴跌
        volume_part = min(1.0, a.volume_ratio / 1.5)
        sentiment_factor = sentiment_factor * 0.7 + volume_part * 0.3
        details.append(f"情绪: 24h{chg:+.1f}% 量比{a.volume_ratio:.2f}")

        score = (
            price_factor * self.WEIGHTS["price"]
            + flow_factor * self.WEIGHTS["flow"]
            + trend_factor * self.WEIGHTS["trend"]
            + volatility_factor * self.WEIGHTS["volatility"]
            + sentiment_factor * self.WEIGHTS["sentiment"]
        ) * 100

        return AlphaScore(
            symbol=a.symbol,
            score=round(score, 1),
            price_factor=price_factor,
            flow_factor=flow_factor,
            trend_factor=trend_factor,
            volatility_factor=volatility_factor,
            sentiment_factor=sentiment_factor,
            details=details,
        )
