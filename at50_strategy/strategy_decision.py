"""
Decision Engine(V3.0)

多策略信号融合决策层:

    Strategy Signals (grid/trend/entry/exit...)
            |
            v
    Decision Engine: 加权融合(策略权重 + regime 调整 + 冲突消解)
            |
            v
    最终唯一动作 BUY / SELL / HOLD

融合规则:
1. 各信号按 (score/100 * 策略权重 * regime 系数) 计算加权票
2. BUY 票 - SELL 票 = 净分; |净分| >= 阈值才行动, 否则 HOLD
3. 高分卖出(exit 清仓类)优先于买入(保命优先)
4. 观察档信号不计票
"""

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from at30_analytics.engine import MarketAnalytics
from at50_strategy.strategy_base import Signal, SignalSide

# 决策依据的 reason 标签(供落库/复盘)
DECISION_REASONS_BUY = "buy_votes"
DECISION_REASONS_SELL = "sell_votes"


@dataclass
class Decision:
    """最终决策"""

    action: str  # BUY / SELL / HOLD
    confidence: float = 0.0  # 0~100
    symbol: str = ""
    side: Optional[SignalSide] = None
    # V7: 数量/金额仅为策略建议参考值(suggested),
    # 最终交易数量必须由 PortfolioAllocator + PositionSizer + Risk 决定
    quantity: Optional[float] = None
    quote_amount: Optional[float] = None
    price: float = 0.0
    votes: list[dict[str, Any]] = field(default_factory=list)  # 各策略投票明细
    net_score: float = 0.0
    reason: list[str] = field(default_factory=list)

    @property
    def actionable(self) -> bool:
        return self.action in ("BUY", "SELL") and self.side is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "confidence": round(self.confidence, 1),
            "symbol": self.symbol,
            "net_score": round(self.net_score, 3),
            "votes": self.votes,
            "reason": self.reason,
        }


class DecisionEngine:
    """多策略融合决策引擎"""

    # 策略基础权重(可被绩效数据动态覆盖)
    DEFAULT_WEIGHTS: dict[str, float] = {
        "entry": 1.0,   # 评分模型,信息最全
        "exit": 1.3,    # 卖出/风控优先加权(保命优先)
        "grid": 0.7,    # 网格: 震荡市高频但单次价值低
        "trend": 0.9,   # 趋势: 捕捉大行情
    }

    # Regime 对买卖方向的系数调整
    REGIME_BUY_FACTOR: dict[str, float] = {
        "BULL": 1.2, "NORMAL": 1.1, "SIDEWAY": 1.0, "VOLATILE": 0.8,
        "BEAR": 0.6, "PANIC": 0.0,
    }
    REGIME_SELL_FACTOR: dict[str, float] = {
        "BULL": 0.8, "NORMAL": 0.9, "SIDEWAY": 1.0, "VOLATILE": 1.1,
        "BEAR": 1.2, "PANIC": 1.3,
    }

    def __init__(
        self,
        action_threshold: float = 0.35,  # 净分阈值(占满票比例)
        weights: Optional[dict[str, float]] = None,
    ):
        self.action_threshold = action_threshold
        self.weights = dict(self.DEFAULT_WEIGHTS)
        if weights:
            self.weights.update(weights)

    def decide(self, signals: list[Signal], analytics: MarketAnalytics) -> Decision:
        """融合多个策略信号 -> 唯一决策"""
        if not signals:
            return Decision(action="HOLD", symbol=analytics.symbol, price=analytics.price)

        regime = analytics.regime or "SIDEWAY"
        buy_factor = self.REGIME_BUY_FACTOR.get(regime, 1.0)
        sell_factor = self.REGIME_SELL_FACTOR.get(regime, 1.0)

        buy_score = 0.0
        sell_score = 0.0
        votes: list[dict[str, Any]] = []

        for sig in signals:
            # 观察档信号不计票
            if any("观察档" in r for r in sig.reason):
                votes.append(
                    {"strategy": sig.strategy, "side": sig.side.value, "weight": 0.0,
                     "note": "observe"}
                )
                continue

            if sig.strategy not in self.weights:
                # V7: 未知策略拒绝计票(交易系统宁可停止也不静默用错参数)
                import structlog

                structlog.get_logger("DecisionEngine").warning(
                    "未知策略无权重, 信号跳过", strategy=sig.strategy
                )
                votes.append(
                    {"strategy": sig.strategy, "side": sig.side.value,
                     "weight": 0.0, "note": "unknown_strategy_skipped"}
                )
                continue
            weight = self.weights[sig.strategy]
            vote = (sig.score / 100.0) * weight

            if sig.side == SignalSide.BUY:
                buy_score += vote * buy_factor
            else:
                sell_score += vote * sell_factor

            votes.append(
                {
                    "strategy": sig.strategy,
                    "side": sig.side.value,
                    "score": sig.score,
                    "weight": weight,
                    "vote": round(vote, 3),
                }
            )

        net = buy_score - sell_score
        total = buy_score + sell_score or 1.0

        # 高分清仓信号优先(exit score>=85 且 sell 侧)
        exit_force = any(
            s.strategy == "exit" and s.side == SignalSide.SELL and s.score >= 85
            for s in signals
        )

        reason: list[str] = []
        if exit_force:
            action = "SELL"
            confidence = min(100.0, sell_score / total * 100)
            reason.append("高分退出信号优先")
        elif net >= self.action_threshold:
            action = "BUY"
            confidence = min(100.0, buy_score / total * 100)
        elif net <= -self.action_threshold:
            action = "SELL"
            confidence = min(100.0, sell_score / total * 100)
        else:
            action = "HOLD"
            confidence = 100.0 - abs(net) / total * 100

        reason.append(f"净分{net:+.2f}(买{buy_score:.2f}/卖{sell_score:.2f}), 阈值±{self.action_threshold}")
        reason.append(f"环境 {regime}(买x{buy_factor:.1f}/卖x{sell_factor:.1f})")

        # 载体信号: BUY 取买方最高分(数量仅作建议参考, 最终由 Sizer 定)
        chosen: Optional[Signal] = None
        if action != "HOLD":
            wanted = SignalSide.BUY if action == "BUY" else SignalSide.SELL
            candidates = [s for s in signals if s.side == wanted]
            if candidates:
                chosen = max(candidates, key=lambda s: s.score)

        return Decision(
            action=action,
            confidence=round(confidence, 1),
            symbol=analytics.symbol,
            side=chosen.side if chosen else None,
            quantity=chosen.quantity if chosen else None,
            quote_amount=chosen.quote_amount if chosen else None,
            price=chosen.price if chosen else analytics.price,
            votes=votes,
            net_score=round(net, 3),
            reason=reason,
        )

    def update_weights(self, performance: dict[str, dict[str, float]]) -> None:
        """按绩效动态调整策略权重(win_rate 高 -> 加权)

        performance: {strategy: {"win_rate": 0.6, "profit": 120.0}}
        """
        for strategy, stats in performance.items():
            if strategy not in self.weights:
                continue
            win_rate = stats.get("win_rate", 0.5)
            # win_rate 0.5 为基准, 偏离线性调整 ±40%
            factor = 0.6 + win_rate * 0.8  # 0.6~1.4
            base = self.DEFAULT_WEIGHTS.get(strategy, 0.5)
            self.weights[strategy] = base * factor
