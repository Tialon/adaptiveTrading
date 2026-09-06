"""
Entry Strategy(V2.0 评分模型)

BUY_SCORE 加权:
- 价格位置(相对近期高低区间)   30%
- VWAP 偏离                    20%
- CVD 趋势                     20%
- 主动买卖比例(delta_ratio)     15%
- 成交量变化                   15%

执行: score >= 80 买入 / 60~80 观察(仅记录) / < 60 禁止
"""

from typing import Optional

from analytics.engine import MarketAnalytics
from common.config.settings import get_settings
from strategy.base import BaseStrategy, Signal, SignalSide

# 观察档:达到该分数仅记录信号不执行
OBSERVE_THRESHOLD = 60.0
BUY_THRESHOLD = 80.0


class BuyStrategy(BaseStrategy):
    """评分买入策略(Entry)"""

    name = "entry"

    def __init__(self, symbols: Optional[list[str]] = None):
        super().__init__(symbols)
        settings = get_settings()
        # V2.0: 目标金额 = 单笔限额的 50%(百分比计算)
        single_quote = settings.risk_max_single_order_quote
        if single_quote <= 0:
            single_quote = settings.risk_initial_equity * settings.risk_max_single_order_pct
        self.quote_amount = single_quote * 0.5
        self.buy_threshold = settings.entry_buy_threshold
        self.observe_threshold = settings.entry_observe_threshold

    def _score_components(self, a: MarketAnalytics) -> list[tuple[str, float, float, str]]:
        """各分项: (名称, 得分0~1, 权重, 说明)"""
        comps: list[tuple[str, float, float, str]] = []

        # 1. 价格位置 30%: 价格接近区间低点得高分(低吸)
        pos_score = 0.5
        if a.recent_low > 0 and a.recent_high > a.recent_low:
            pos_score = max(0.0, min(1.0, (a.recent_high - a.price) / (a.recent_high - a.recent_low)))
        comps.append((
            "价格位置", pos_score, 0.30,
            f"区间[{a.recent_low:.2f},{a.recent_high:.2f}]位置{(1-pos_score):.0%}低吸",
        ))

        # 2. VWAP 偏离 20%: 折价越高分越高(偏离 -2% 满分)
        dip = max(0.0, -a.vwap_deviation)
        vwap_score = min(1.0, dip / 0.02) if a.vwap > 0 else 0.0
        comps.append((
            "VWAP偏离", vwap_score, 0.20,
            f"低于VWAP {dip:.2%}" if dip > 0 else f"高于VWAP {-a.vwap_deviation:.2%}",
        ))

        # 3. CVD 20%: 上升强度(斜率归一,>=0.5 满分)
        cvd_score = min(1.0, max(0.0, a.cvd_slope / 0.5)) if a.cvd_rising else 0.0
        comps.append((
            "CVD", cvd_score, 0.20,
            "CVD上升" if a.cvd_rising else "CVD下行",
        ))

        # 4. 主动买卖比 15%: delta_ratio >= 0.2 满分
        delta_score = min(1.0, max(0.0, a.delta_ratio / 0.2))
        comps.append((
            "买卖比", delta_score, 0.15,
            f"主动买占比{a.delta_ratio:.1%}",
        ))

        # 5. 成交量变化 15%: 近期量比(>1.3 放量满分,萎缩 0 分)
        vol_score = 0.0
        if a.volume_ratio >= 1.3:
            vol_score = 1.0
        elif a.volume_ratio >= 1.0:
            vol_score = 0.5
        comps.append((
            "量能", vol_score, 0.15,
            f"量比{a.volume_ratio:.2f}",
        ))

        return comps

    def on_market(self, a: MarketAnalytics) -> list[Signal]:
        if a.symbol not in self.symbols or a.price <= 0:
            return []
        if not self._cooldown_ok(a.symbol):
            return []

        comps = self._score_components(a)
        score = sum(s * w for _, s, w, _ in comps) * 100  # 0~100
        reasons = [f"{name}:{desc}(得{int(s*100)}/100,权重{int(w*100)}%)" for name, s, w, desc in comps]

        indicators = {
            "vwap": round(a.vwap, 4),
            "vwap_deviation": round(a.vwap_deviation, 5),
            "cvd": round(a.cvd, 2),
            "cvd_slope": round(a.cvd_slope, 4),
            "cvd_rising": a.cvd_rising,
            "delta_ratio": round(a.delta_ratio, 4),
            "volume_ratio": round(a.volume_ratio, 4),
            "recent_high": a.recent_high,
            "recent_low": a.recent_low,
            "regime": a.regime,
        }

        if score < self.observe_threshold:
            return []  # 分数不足,不产生信号

        self._mark_signal(a.symbol)
        signal = Signal(
            symbol=a.symbol,
            strategy=self.name,
            side=SignalSide.BUY,
            price=a.price,
            quote_amount=self.quote_amount,
            reason=reasons,
            score=round(score, 1),
            indicators=indicators,
        )
        if score < self.buy_threshold:
            # 观察档:标记不执行
            signal.reason.insert(0, f"观察档({score:.0f}分,未达{self.buy_threshold})")
        return [signal]
