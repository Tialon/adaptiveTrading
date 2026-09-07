"""
Funding + OI 情绪因子(V9.0 M3.4) — 可选, 默认关闭

由合约 Funding Rate + Open Interest 变化率合成情绪分:
- 高 funding = 拥挤多头 = 反向偏空(funding 越正, 越偏空)
- OI 升 = 新资金进场 = 偏多
输出 {funding_rate, oi, oi_change_pct, score, bias}。

不碰现货主链路, 默认 sentiment_enabled=false。
"""

from dataclasses import dataclass
from typing import Any, Optional

from at01_common.logger import LoggerMixin
from at01_common.settings import get_settings

# OI 变化率满分阈值(5% 即视为显著资金进出)
OI_CHANGE_FULL = 0.05


def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


@dataclass
class SentimentResult:
    funding_rate: float
    oi: float
    oi_change_pct: float
    score: float  # -1(极度偏空) ~ +1(极度偏多)
    bias: str  # bullish / neutral / bearish

    def to_dict(self) -> dict[str, Any]:
        return {
            "funding_rate": self.funding_rate,
            "oi": self.oi,
            "oi_change_pct": round(self.oi_change_pct, 4),
            "score": round(self.score, 3),
            "bias": self.bias,
        }


def compute_sentiment(
    funding_rate: float,
    oi: float,
    prev_oi: Optional[float],
    funding_threshold: float = 0.0005,
) -> SentimentResult:
    """由 funding + OI 变化率合成情绪分(纯函数, 便于测试)

    - funding 项: 拥挤多头反向 —— funding/threshold 越正, 越偏空。
    - OI 项: 新资金进场 —— OI 变化率 / 5% 越正, 越偏多。
    """
    oi_change_pct = 0.0
    if prev_oi is not None and prev_oi > 0:
        oi_change_pct = (oi - prev_oi) / prev_oi

    funding_score = -_clamp(funding_rate / funding_threshold if funding_threshold > 0 else 0.0)
    oi_score = _clamp(oi_change_pct / OI_CHANGE_FULL)
    score = _clamp(0.5 * funding_score + 0.5 * oi_score)

    if score > 0.2:
        bias = "bullish"
    elif score < -0.2:
        bias = "bearish"
    else:
        bias = "neutral"

    return SentimentResult(
        funding_rate=funding_rate,
        oi=oi,
        oi_change_pct=oi_change_pct,
        score=score,
        bias=bias,
    )


class SentimentAnalyzer(LoggerMixin):
    """情绪因子分析器(轮询 funding + OI, 维护 prev_oi)"""

    def __init__(self, client=None, funding_threshold: Optional[float] = None):
        self.settings = get_settings()
        self.client = client
        self.funding_threshold = funding_threshold or self.settings.sentiment_funding_threshold
        self._prev_oi: Optional[float] = None

    async def poll(self, symbol: str) -> Optional[SentimentResult]:
        """轮询一次; 关闭时返回 None(不产出)"""
        if not self.settings.sentiment_enabled:
            return None
        if self.client is None:
            return None
        try:
            funding = await self.client.get_funding_rate(symbol)
            oi = await self.client.get_open_interest(symbol)
            prev = self._prev_oi
            self._prev_oi = oi
            result = compute_sentiment(funding, oi, prev, self.funding_threshold)
            self.logger.info("情绪因子", symbol=symbol, **result.to_dict())
            return result
        except Exception:
            self.logger.exception("情绪因子获取失败")
            return None
