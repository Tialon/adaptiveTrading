"""
Market Regime Engine(V2.0)

识别市场环境: BULL / NORMAL / SIDEWAY / VOLATILE / BEAR / PANIC

输入(来自 Analytics Engine 与行情引擎):
- BTC 趋势(大盘锚)
- 标的趋势(EMA 快慢)
- 波动率(近期高低区间振幅)
- 成交量(量比)
- 资金流(delta_ratio / CVD)

输出去向:
- AnalyticsEngine.set_regime -> 策略评分参考
- 策略自动调整: BULL 减少卖出/趋势策略, SIDEWAY 网格高抛低吸,
  BEAR 降仓停补, PANIC 暂停交易
"""

import time
from dataclasses import dataclass
from typing import Any, Optional

from at01_common.logger import LoggerMixin


@dataclass
class RegimeAssessment:
    """市场环境评估"""

    regime: str  # BULL / NORMAL / SIDEWAY / VOLATILE / BEAR / PANIC
    btc_trend: str = "neutral"
    symbol_trend: str = "neutral"
    volatility: float = 0.0  # 近期振幅
    volume_ratio: float = 1.0
    flow_score: float = 0.0  # 资金流综合分 -1~1
    confidence: float = 0.0  # 0~1
    reasons: Optional[list[str]] = None

    def __post_init__(self):
        if self.reasons is None:
            self.reasons = []

    def to_dict(self) -> dict[str, Any]:
        return {
            "regime": self.regime,
            "btc_trend": self.btc_trend,
            "symbol_trend": self.symbol_trend,
            "volatility": round(self.volatility, 4),
            "volume_ratio": round(self.volume_ratio, 3),
            "flow_score": round(self.flow_score, 3),
            "confidence": round(self.confidence, 2),
            "reasons": self.reasons,
        }


class MarketRegimeEngine(LoggerMixin):
    """市场环境引擎"""

    # 环境判定阈值
    VOLATILITY_PANIC = 0.03  # 振幅 ≥3% 视为剧烈
    VOLATILITY_HIGH = 0.015  # ≥1.5% 波动偏大
    VOLATILITY_NORMAL = 0.010  # <1.0% 平静(V9.0: 中性区三档细分)

    def __init__(self, watch_interval: float = 30.0):
        self.watch_interval = watch_interval
        self._latest: dict[str, RegimeAssessment] = {}
        self._last_eval: float = 0.0

    def evaluate(
        self,
        symbol: str,
        symbol_trend: str,
        symbol_ema_fast: float,
        symbol_ema_slow: float,
        recent_high: float,
        recent_low: float,
        volume_ratio: float,
        delta_ratio: float,
        cvd_rising: bool,
        btc_trend: str = "neutral",
        btc_change_24h: float = 0.0,
    ) -> RegimeAssessment:
        """综合评估市场环境"""
        reasons: list[str] = []

        # --- 波动率 ---
        volatility = 0.0
        if recent_high > 0 and recent_low > 0:
            volatility = (recent_high - recent_low) / ((recent_high + recent_low) / 2)
        reasons.append(f"振幅{volatility:.2%}")

        # --- 资金流 ---
        flow_score = 0.0
        if delta_ratio > 0.05:
            flow_score += 0.5
        elif delta_ratio < -0.05:
            flow_score -= 0.5
        if cvd_rising:
            flow_score += 0.5
        flow_score = max(-1.0, min(1.0, flow_score))
        reasons.append(f"资金流{flow_score:+.1f}")

        # --- 趋势票 ---
        trend_votes = 0
        if symbol_trend == "up":
            trend_votes += 1
        elif symbol_trend == "down":
            trend_votes -= 1
        if btc_trend == "up":
            trend_votes += 1
        elif btc_trend == "down":
            trend_votes -= 1
        if btc_change_24h > 2.0:
            trend_votes += 1
        elif btc_change_24h < -2.0:
            trend_votes -= 1

        # --- 判定 ---
        if volatility >= self.VOLATILITY_PANIC and (volume_ratio > 2.0 or trend_votes <= -2):
            regime = "PANIC"
            reasons.append("剧烈波动+放量,恐慌")
            confidence = 0.8
        elif trend_votes >= 2 and flow_score > 0:
            regime = "BULL"
            reasons.append("多周期趋势向上+资金流入")
            confidence = 0.75
        elif trend_votes <= -2 and flow_score < 0:
            regime = "BEAR"
            reasons.append("多周期趋势向下+资金流出")
            confidence = 0.75
        elif volatility >= self.VOLATILITY_HIGH:
            regime = "VOLATILE"
            reasons.append("波动偏大,宽幅震荡")
            confidence = 0.55
        elif volatility >= self.VOLATILITY_NORMAL:
            regime = "SIDEWAY"
            reasons.append("趋势中性,普通盘整")
            confidence = 0.45
        else:
            regime = "NORMAL"
            reasons.append("波动平缓,平静健康")
            confidence = 0.40

        result = RegimeAssessment(
            regime=regime,
            btc_trend=btc_trend,
            symbol_trend=symbol_trend,
            volatility=volatility,
            volume_ratio=volume_ratio,
            flow_score=flow_score,
            confidence=confidence,
            reasons=reasons,
        )
        self._latest[symbol] = result
        return result

    # ---------- 策略调整建议 ----------

    @staticmethod
    def strategy_adjustment(regime: str) -> dict[str, Any]:
        """各环境下的策略调整(策略引擎读取)"""
        adjustments = {
            "BULL": {
                "sell_enabled": True,
                "buy_boost": 1.2,  # 买入分数加成
                "grid_enabled": True,
                "trend_weight": 1.0,
                "add_position_allowed": True,
                "note": "牛市: 减少过早止盈, 趋势跟随为主",
            },
            "NORMAL": {
                "sell_enabled": True,
                "buy_boost": 1.0,
                "grid_enabled": True,
                "trend_weight": 0.7,
                "add_position_allowed": True,
                "note": "平静: 低波中性, 温和参与",
            },
            "SIDEWAY": {
                "sell_enabled": True,
                "buy_boost": 1.0,
                "grid_enabled": True,  # 震荡以网格为主
                "trend_weight": 0.5,
                "add_position_allowed": True,
                "note": "震荡: 网格高抛低吸",
            },
            "VOLATILE": {
                "sell_enabled": True,
                "buy_boost": 0.6,
                "grid_enabled": False,  # 高波震荡关网格, 避免反复打脸
                "trend_weight": 0.4,
                "add_position_allowed": False,
                "note": "宽幅震荡: 关网格, 谨慎",
            },
            "BEAR": {
                "sell_enabled": True,
                "buy_boost": 0.6,  # 买入门槛变相提高
                "grid_enabled": False,
                "trend_weight": 1.0,
                "add_position_allowed": False,  # 停止补仓
                "note": "熊市: 降仓, 停止补仓",
            },
            "PANIC": {
                "sell_enabled": True,
                "buy_boost": 0.0,  # 禁止买入
                "grid_enabled": False,
                "trend_weight": 0.0,
                "add_position_allowed": False,
                "note": "恐慌: 暂停交易, 只减不加",
            },
        }
        return adjustments.get(regime, adjustments["SIDEWAY"])

    # ---------- 查询 ----------

    def get(self, symbol: str) -> Optional[RegimeAssessment]:
        return self._latest.get(symbol)

    def snapshot(self) -> dict[str, Any]:
        return {s: r.to_dict() for s, r in self._latest.items()}
