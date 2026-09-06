"""
吸筹(Accumulation)检测

启发式规则(时间窗口内):
1. 价格横盘:波动幅度 < accumulation_flat_pct
2. 净流入为正:主动买入量 > 主动卖出量
3. 大单偏向买方:大单中买入占比高
4. CVD 上升

满足条件越多,吸筹分数越高(0~1)。
"""

import time
from collections import deque
from dataclasses import dataclass, field

from at20_market.market_models import TradeTick


@dataclass
class AccumulationResult:
    """吸筹检测结果"""

    score: float = 0.0  # 0~1
    is_accumulating: bool = False
    price_range_pct: float = 0.0  # 窗口内价格振幅
    net_flow: float = 0.0  # 净主动流入(USDT)
    whale_buy_quote: float = 0.0
    whale_sell_quote: float = 0.0
    samples: int = 0
    reasons: list[str] = field(default_factory=list)


class AccumulationDetector:
    """吸筹检测器(时间窗口,默认60秒)"""

    def __init__(
        self,
        window_seconds: int = 60,
        flat_pct: float = 0.003,  # 振幅 < 0.3% 视为横盘
        whale_threshold: float = 50000.0,
        min_samples: int = 20,
    ):
        self.window_seconds = window_seconds
        self.flat_pct = flat_pct
        self.whale_threshold = whale_threshold
        self.min_samples = min_samples
        self._ticks: deque[TradeTick] = deque()

    def update(self, tick: TradeTick, now_ms: int | None = None) -> AccumulationResult:
        """喂入成交并返回窗口内吸筹评估"""
        now = now_ms if now_ms is not None else tick.trade_time
        self._ticks.append(tick)

        # 淘汰窗口外数据
        cutoff = now - self.window_seconds * 1000
        while self._ticks and self._ticks[0].trade_time < cutoff:
            self._ticks.popleft()

        return self._evaluate()

    def _evaluate(self) -> AccumulationResult:
        result = AccumulationResult(samples=len(self._ticks))
        if len(self._ticks) < self.min_samples:
            return result

        prices = [t.price for t in self._ticks]
        hi, lo = max(prices), min(prices)
        mid = (hi + lo) / 2
        result.price_range_pct = (hi - lo) / mid if mid > 0 else 0.0

        buy_quote = sum(t.quote_quantity for t in self._ticks if not t.is_buyer_maker)
        sell_quote = sum(t.quote_quantity for t in self._ticks if t.is_buyer_maker)
        result.net_flow = buy_quote - sell_quote

        whales = [t for t in self._ticks if t.quote_quantity >= self.whale_threshold]
        result.whale_buy_quote = sum(
            t.quote_quantity for t in whales if not t.is_buyer_maker
        )
        result.whale_sell_quote = sum(t.quote_quantity for t in whales if t.is_buyer_maker)

        score = 0.0
        reasons: list[str] = []

        # 规则1:横盘
        if result.price_range_pct < self.flat_pct:
            score += 0.25
            reasons.append("价格横盘")
        else:
            reasons.append(f"波动{result.price_range_pct:.3%}")

        # 规则2:净流入为正
        total = buy_quote + sell_quote
        if total > 0 and result.net_flow > 0:
            flow_ratio = result.net_flow / total
            score += min(0.3, 0.15 + flow_ratio)
            reasons.append(f"净流入{flow_ratio:.1%}")
        else:
            reasons.append("净流出")

        # 规则3:大单买方主导
        whale_total = result.whale_buy_quote + result.whale_sell_quote
        if whale_total > 0:
            whale_buy_ratio = result.whale_buy_quote / whale_total
            if whale_buy_ratio > 0.6:
                score += 0.25
                reasons.append(f"大单买入占比{whale_buy_ratio:.0%}")
            else:
                reasons.append(f"大单买占比{whale_buy_ratio:.0%}")
        else:
            score += 0.1  # 无大单抛压,轻微加分
            reasons.append("无大单活动")

        # 规则4:CVD 趋势(用净流入斜率近似:后半段流入 > 前半段)
        ticks = list(self._ticks)
        n = len(ticks)
        half = n // 2
        first_delta = sum(
            t.quote_quantity * (1 if not t.is_buyer_maker else -1)
            for t in ticks[:half]
        )
        second_delta = sum(
            t.quote_quantity * (1 if not t.is_buyer_maker else -1)
            for t in ticks[half:]
        )
        if second_delta > first_delta:
            score += 0.2
            reasons.append("买压增强")

        result.score = min(1.0, score)
        result.is_accumulating = result.score >= 0.6
        result.reasons = reasons
        return result
