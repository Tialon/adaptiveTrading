"""资金级 Circuit Breaker(V11.1 P1-5)

Equity / Position / Cash 三向漂移分级处置(0.1% / 0.2% / 0.5%), 取代「一漂移就冻结」的
粗粒度做法: 小漂移先降级(REDUCE_ONLY 只减仓), 逐级收紧到 PAUSE / KILL。

分级表(drift_pct 为绝对漂移比例, 0.003 = 0.3%):

    | 漂移率    | Equity      | Position / Cash |
    |-----------|-------------|-----------------|
    | < 0.1%    | NONE        | NONE            |
    | 0.1~0.2%  | REDUCE_ONLY | REDUCE_ONLY     |
    | 0.2~0.5%  | PAUSE       | REDUCE_ONLY     |
    | > 0.5%    | KILL        | PAUSE           |

规则:
- Position / Cash 漂移 → 首选 REDUCE_ONLY(减仓去险, 不贸然冻结), 仅 >0.5% 才 PAUSE。
- Equity(总权益)漂移是资金级最严重信号 → 0.1% 起 REDUCE_ONLY、0.2% PAUSE、0.5% KILL。
- 三向独立分级, 最终动作取最严重一档(severity: NONE < REDUCE_ONLY < PAUSE < KILL)。

纯函数 `classify_drift` 可独立测试; `FundCircuitBreaker.assess` 聚合三向。
"""

from dataclasses import dataclass, field
from enum import Enum

from at01_common.logger import LoggerMixin


class BreakerAction(str, Enum):
    NONE = "NONE"
    REDUCE_ONLY = "REDUCE_ONLY"
    PAUSE = "PAUSE"
    KILL = "KILL"


# 严重度排序(用于取三向最严重)
_SEVERITY = {
    BreakerAction.NONE: 0,
    BreakerAction.REDUCE_ONLY: 1,
    BreakerAction.PAUSE: 2,
    BreakerAction.KILL: 3,
}

# 三档阈值
_T1 = 0.001  # 0.1%
_T2 = 0.002  # 0.2%
_T3 = 0.005  # 0.5%

_POSITION_LIKE = ("position", "cash")


def classify_drift(dimension: str, drift_pct: float) -> BreakerAction:
    """按维度 + 漂移率分级(纯函数)。drift_pct 为绝对漂移比例(0.003 = 0.3%)。"""
    if drift_pct < _T1:
        return BreakerAction.NONE
    if dimension in _POSITION_LIKE:
        if drift_pct >= _T3:
            return BreakerAction.PAUSE
        return BreakerAction.REDUCE_ONLY
    # equity
    if drift_pct >= _T3:
        return BreakerAction.KILL
    if drift_pct >= _T2:
        return BreakerAction.PAUSE
    return BreakerAction.REDUCE_ONLY


def drift_pct(diff: float, base: float) -> float:
    """漂移比例 = |diff| / base(base <= 0 → 0)。"""
    return abs(diff) / base if base > 0 else 0.0


@dataclass
class BreakerDecision:
    """三向漂移处置决策。"""

    action: BreakerAction = BreakerAction.NONE
    dimensions: dict[str, BreakerAction] = field(default_factory=dict)
    reason: str = ""

    @property
    def actionable(self) -> bool:
        return self.action is not BreakerAction.NONE

    def to_dict(self) -> dict:
        return {
            "action": self.action.value,
            "dimensions": {k: v.value for k, v in self.dimensions.items()},
            "reason": self.reason,
        }


class FundCircuitBreaker(LoggerMixin):
    """资金级熔断器: 三向漂移聚合为单一处置动作(取最严重一档)。"""

    def assess(
        self,
        equity_drift: float = 0.0,
        position_drift: float = 0.0,
        cash_drift: float = 0.0,
    ) -> BreakerDecision:
        dims = {
            "equity": classify_drift("equity", equity_drift),
            "position": classify_drift("position", position_drift),
            "cash": classify_drift("cash", cash_drift),
        }
        action = max(dims.values(), key=lambda a: _SEVERITY[a])
        triggered = [f"{k}={v.value}" for k, v in dims.items() if v is not BreakerAction.NONE]
        reason = ", ".join(triggered) if triggered else ""
        decision = BreakerDecision(action=action, dimensions=dims, reason=reason)
        if decision.actionable:
            self.logger.warning("资金级熔断判定", **decision.to_dict())
        return decision
