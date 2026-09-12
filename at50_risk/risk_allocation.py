"""
Portfolio Allocation Engine(V4.0)

从"交易机器人"升级为"SOL 主动资产管理系统"的核心:

    Market Regime + Confidence
            |
            v
    Target Exposure(总敞口 0~1)
            |
            v
    Core / Trade 双仓分配
            |
            v
    Rebalance 建议(偏离容忍带)

敞口表(2 万本金 / SOL 单币 / 长期持有导向):
    strong_bull  0.90   (牛市 80-90% 仓位)
    bull         0.75
    neutral      0.50
    bear         0.25   (熊市 20-30% 仓位)
    panic        0.10

核心仓/交易仓比例: 核心仓 = 总仓位的 70%(长期), 交易仓 30%(高抛低吸)。
"""

from dataclasses import dataclass, field
from typing import Any, Optional

from at01_common.logger import LoggerMixin

# V4: 更细粒度的 regime(兼容 V2 的四态输入, strong_bull 由 confidence 区分)
# V9.0: 扩展为 6 态(BULL/NORMAL/SIDEWAY/VOLATILE/BEAR/PANIC)
EXPOSURE_TABLE = {
    "strong_bull": 0.90,
    "BULL": 0.75,
    "NORMAL": 0.60,
    "SIDEWAY": 0.50,
    "VOLATILE": 0.35,
    "BEAR": 0.25,
    "PANIC": 0.10,
}

# 置信度区间 -> regime 强度细分(同 regime, 高置信 = 强档)
STRONG_CONFIDENCE = 0.75  # BULL 且 confidence >= 0.75 视为 strong_bull

# 核心仓 / 交易仓 比例
CORE_RATIO = 0.70
TRADE_RATIO = 0.30


@dataclass
class AllocationPlan:
    """资产配置计划"""

    symbol: str
    regime: str = "SIDEWAY"
    confidence: float = 0.5
    target_exposure: float = 0.5  # 总敞口(SOL 市值/权益)
    target_core_qty: float = 0.0  # 核心仓目标(数量)
    target_trade_qty: float = 0.0  # 交易仓目标(数量)
    current_core_qty: float = 0.0
    current_trade_qty: float = 0.0
    core_diff: float = 0.0  # 目标-当前(正=加仓)
    trade_diff: float = 0.0
    rebalance_needed: bool = False
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "regime": self.regime,
            "confidence": round(self.confidence, 2),
            "target_exposure": round(self.target_exposure, 3),
            "target_core_qty": round(self.target_core_qty, 4),
            "target_trade_qty": round(self.target_trade_qty, 4),
            "current_core_qty": round(self.current_core_qty, 4),
            "current_trade_qty": round(self.current_trade_qty, 4),
            "core_diff": round(self.core_diff, 4),
            "trade_diff": round(self.trade_diff, 4),
            "rebalance_needed": self.rebalance_needed,
            "reasons": self.reasons,
        }


class PortfolioAllocator(LoggerMixin):
    """资产配置引擎(动态敞口 + 双仓)"""

    def __init__(
        self,
        initial_equity: float = 20000.0,
        rebalance_tolerance: float = 0.05,  # 偏离 >5% 触发再平衡
        core_ratio: Optional[float] = None,  # V9.0: 核心仓占仓位比例(默认沿用模块常量, 向后兼容)
        trade_ratio: Optional[float] = None,  # V9.0: 交易仓占仓位比例
    ):
        self.initial_equity = initial_equity
        self.rebalance_tolerance = rebalance_tolerance
        self.core_ratio = core_ratio if core_ratio is not None else CORE_RATIO
        self.trade_ratio = trade_ratio if trade_ratio is not None else TRADE_RATIO

    def risk_adjustment_factor(
        self,
        volatility: float = 0.0,
        btc_change_24h: float = 0.0,
        btc_trend: str = "neutral",
        drawdown: float = 0.0,
    ) -> float:
        """V5: 风险调整因子(乘到目标敞口上, 0.5~1.0)

        - 波动率: 振幅 >3% 强降 / >1.5% 中降
        - BTC: 24h 跌 >4% 或趋势 down 降
        - 回撤: 分级线性收紧
        """
        factor = 1.0

        if volatility > 0.03:
            factor *= 0.6
        elif volatility > 0.015:
            factor *= 0.8

        if btc_change_24h <= -4.0 or btc_trend == "down":
            factor *= 0.8
        elif btc_change_24h >= 4.0 and btc_trend == "up":
            factor *= 1.05

        if drawdown > 0.30:
            factor *= 0.5
        elif drawdown > 0.20:
            factor *= 0.7
        elif drawdown > 0.10:
            factor *= 0.85

        return max(0.4, min(1.0, factor))

    def target_exposure(self, regime: str, confidence: float) -> float:
        """regime × confidence -> 目标敞口

        强牛: 90% × confidence 加权; 置信不足向 neutral 收敛
        """
        base = EXPOSURE_TABLE.get(regime, 0.5)
        # confidence 0.5 为中性: 高置信放大敞口变化, 低置信向 50% 收敛
        weighted = 0.5 + (base - 0.5) * (0.5 + confidence / 2.0)
        return max(0.05, min(0.95, weighted))

    def refine_regime(self, regime: str, confidence: float) -> str:
        """BULL 高置信 -> strong_bull(敞口上调)"""
        if regime == "BULL" and confidence >= STRONG_CONFIDENCE:
            return "strong_bull"
        return regime

    def plan(
        self,
        symbol: str,
        regime: str,
        confidence: float,
        equity: float,
        market_price: float,
        current_core_qty: float = 0.0,
        current_trade_qty: float = 0.0,
        risk_factor: float = 1.0,  # V5: risk_adjustment_factor 输出
    ) -> AllocationPlan:
        """生成配置计划(含双仓再平衡建议)"""
        refined = self.refine_regime(regime, confidence)
        exposure = self.target_exposure(refined, confidence) * risk_factor

        reasons = [f"环境 {refined}(置信{confidence:.0%}) -> 目标敞口 {exposure:.0%}"]

        target_value = equity * exposure
        total_qty = target_value / market_price if market_price > 0 else 0.0
        target_core = total_qty * self.core_ratio
        target_trade = total_qty * self.trade_ratio

        current_total = current_core_qty + current_trade_qty
        current_value = current_total * market_price
        current_exposure = current_value / equity if equity > 0 else 0.0

        core_diff = target_core - current_core_qty
        trade_diff = target_trade - current_trade_qty

        # 再平衡判定: 敞口偏离容忍带
        rebalance = abs(current_exposure - exposure) > self.rebalance_tolerance
        if rebalance:
            reasons.append(
                f"敞口偏离 {abs(current_exposure-exposure):.0%} > 容忍带 {self.rebalance_tolerance:.0%}"
            )

        # 大幅减仓档保护(熊市/恐慌, 只减不加)
        if refined in ("BEAR", "PANIC") and core_diff > 0:
            core_diff = 0.0
            trade_diff = 0.0
            reasons.append("熊市/恐慌: 停止加仓")

        return AllocationPlan(
            symbol=symbol,
            regime=refined,
            confidence=confidence,
            target_exposure=exposure,
            target_core_qty=target_core,
            target_trade_qty=target_trade,
            current_core_qty=current_core_qty,
            current_trade_qty=current_trade_qty,
            core_diff=core_diff,
            trade_diff=trade_diff,
            rebalance_needed=rebalance,
            reasons=reasons,
        )
