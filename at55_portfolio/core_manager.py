"""
Core Position Manager(V9.0) — 核心仓低频管理

核心仓(长期持有)的 ADD / REDUCE / HOLD 决策 + Trend Break Protection。

设计原则(可解释):
- 建仓/加仓(全满足才 ADD): BTC 锚不弱 / SOL 站上长期趋势(EMA 多头排列) / 非 PANIC
- Trend Break Protection(退出, 非止盈): 长期趋势破坏(EMA 死叉) / BTC 锚失败 / PANIC
- 复用现有输入: MarketAnalytics(trend/ema_fast/ema_slow) + RegimeAssessment(btc_trend/regime)

核心仓不同于交易仓: 不追求高抛低吸, 只在趋势破坏时减仓, 属于低频主动管理。
"""

import time
from enum import Enum
from typing import Any, Optional

from at01_common.logger import LoggerMixin
from at01_common.settings import get_settings
from at60_risk.risk_buckets import BucketPositionManager


class CoreAction(str, Enum):
    ADD = "ADD"
    REDUCE = "REDUCE"
    HOLD = "HOLD"


class CorePositionManager(LoggerMixin):
    """核心仓管理器"""

    def __init__(self, buckets: BucketPositionManager):
        self.settings = get_settings()
        self.buckets = buckets
        self._last_decision_ts = 0.0

    def decide(
        self,
        symbol: str,
        analytics: Optional[Any],
        assessment: Optional[Any],
        market_price: float,
        target_core_qty: float,
        btc_change_24h: float = 0.0,
    ) -> dict[str, Any]:
        """核心仓决策 -> {action, reason, add_qty/reduce_qty}"""
        now = time.time()
        if now - self._last_decision_ts < self.settings.core_manager_min_interval_seconds:
            return {"action": CoreAction.HOLD, "reason": "决策冷却中(低频)"}

        regime = assessment.regime if assessment else (
            analytics.regime if analytics else "SIDEWAY"
        )
        btc_trend = assessment.btc_trend if assessment else "neutral"
        sol_trend = analytics.trend if analytics else "neutral"
        ema_fast = analytics.ema_fast if analytics else 0.0
        ema_slow = analytics.ema_slow if analytics else 0.0
        cur_core = self.buckets.core(symbol)

        self._last_decision_ts = now

        # ---- Trend Break Protection(优先级最高, 非止盈) ----
        protection = self._trend_break_reason(
            regime, btc_trend, btc_change_24h, ema_fast, ema_slow
        )
        if protection and cur_core > 0:
            return {
                "action": CoreAction.REDUCE,
                "reason": protection,
                "reduce_qty": cur_core,
            }

        # ---- 建仓/加仓条件 ----
        if cur_core >= target_core_qty:
            return {"action": CoreAction.HOLD, "reason": "核心仓已达目标"}
        if btc_trend == "down" or btc_change_24h <= -self.settings.core_manager_btc_fail_pct:
            return {"action": CoreAction.HOLD, "reason": "BTC 锚走弱, 不建仓"}
        if regime == "PANIC":
            return {"action": CoreAction.HOLD, "reason": "PANIC 环境不建仓"}
        if ema_slow <= 0 or ema_fast <= ema_slow:
            return {"action": CoreAction.HOLD, "reason": "SOL 未站上长期趋势"}
        if market_price <= 0:
            return {"action": CoreAction.HOLD, "reason": "无有效价格"}

        add_qty = target_core_qty - cur_core
        return {"action": CoreAction.ADD, "reason": "核心仓加仓(趋势向上)", "add_qty": add_qty}

    def _trend_break_reason(
        self,
        regime: str,
        btc_trend: str,
        btc_change_24h: float,
        ema_fast: float,
        ema_slow: float,
    ) -> Optional[str]:
        """趋势破坏保护(退出信号, 非止盈)"""
        if regime == "PANIC":
            return "PANIC 环境: 核心仓趋势破坏保护"
        if btc_trend == "down" and btc_change_24h <= -self.settings.core_manager_btc_fail_pct:
            return "BTC 锚失败: 趋势向下且跌幅超阈值"
        if 0 < ema_fast <= ema_slow:
            return "长期趋势破坏: EMA 死叉"
        return None
