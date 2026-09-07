"""
Position Size 模型(V4.0)

Signal 评分 -> 买入比例(决定"买多少"):

    position_ratio = base_ratio
                     × alpha_score / 100        (机会质量)
                     × regime_factor            (环境)
                     × tiered_factor            (回撤档位)
                     × decision_confidence/100  (融合置信)

    buy_quote = equity × position_ratio

上限约束: 单笔不超过权益 5%(单笔限额), 不超过敞口目标缺口。
牛市 85 分 -> 10%;熊市 85 分 -> 2%(regime_factor 0.25)。
"""

from typing import Any, Optional

from at01_common.logger import LoggerMixin

# 各 regime 的买入规模系数
REGIME_SIZE_FACTOR = {
    "strong_bull": 1.2,
    "BULL": 1.0,
    "NORMAL": 0.8,
    "SIDEWAY": 0.6,
    "VOLATILE": 0.4,
    "BEAR": 0.25,
    "PANIC": 0.0,
}


class PositionSizer(LoggerMixin):
    """评分定仓引擎"""

    def __init__(
        self,
        base_ratio: float = 0.10,  # 基准: 满分信号买 10% 权益
        max_single_pct: float = 0.05,  # 单笔硬上限(默认, 会被动态限额覆盖)
        min_notional: float = 10.0,
    ):
        self.base_ratio = base_ratio
        self.max_single_pct = max_single_pct
        self.min_notional = min_notional

    def dynamic_trade_limit(
        self,
        regime: str,
        drawdown: float = 0.0,
        volatility: float = 0.0,
    ) -> float:
        """V5: 动态单笔限额

        牛市 5-10% / 震荡 3-5% / 熊市 0-3%, 再按回撤/波动收紧。
        """
        base = {
            "strong_bull": 0.10,
            "BULL": 0.07,
            "NORMAL": 0.05,
            "SIDEWAY": 0.04,
            "VOLATILE": 0.03,
            "BEAR": 0.02,
            "PANIC": 0.0,
        }.get(regime, 0.04)

        if drawdown > 0.30:
            base *= 0.3
        elif drawdown > 0.20:
            base *= 0.5
        elif drawdown > 0.10:
            base *= 0.7

        if volatility > 0.03:
            base *= 0.6
        elif volatility > 0.015:
            base *= 0.8

        return max(0.0, min(0.10, base))

    def size(
        self,
        decision_score: float,
        alpha_score: float,
        regime: str,
        equity: float,
        price: float,
        tiered_factor: float = 1.0,
        exposure_room_quote: float = 0.0,  # 敞口缺口(目标市值-当前市值)
    ) -> dict[str, Any]:
        """计算买入金额与数量

        返回 {quote, quantity, position_ratio, detail}
        """
        regime_factor = REGIME_SIZE_FACTOR.get(regime, 0.5)
        alpha_part = max(0.0, min(1.0, alpha_score / 100.0))
        confidence_part = max(0.0, min(1.0, decision_score / 100.0))

        # V8: AI 审批通过的仓位比例覆盖基准(运行时参数)
        from at50_strategy.ai_parameter_guard import RuntimeParams

        base_ratio = RuntimeParams.get("position_ratio") or self.base_ratio

        ratio = (
            base_ratio
            * alpha_part
            * regime_factor
            * tiered_factor
            * confidence_part
        )
        quote = equity * ratio

        # 约束 1: 单笔上限(V5: 动态限额)
        cap = equity * self.dynamic_trade_limit(regime)
        if quote > cap:
            quote = cap
        # 约束 2: 敞口缺口
        if exposure_room_quote > 0 and quote > exposure_room_quote:
            quote = exposure_room_quote
        # 约束 3: 最小名义
        if quote < self.min_notional:
            return {"quote": 0.0, "quantity": 0.0, "position_ratio": 0.0,
                    "detail": f"金额过小({quote:.1f}<{self.min_notional})"}

        quantity = quote / price if price > 0 else 0.0
        return {
            "quote": round(quote, 2),
            "quantity": quantity,
            "position_ratio": round(ratio, 4),
            "detail": (
                f"基准{base_ratio:.0%}×Alpha{alpha_score:.0f}×"
                f"环境{regime}({regime_factor})×回撤档({tiered_factor})×"
                f"置信{decision_score:.0f}"
            ),
        }
