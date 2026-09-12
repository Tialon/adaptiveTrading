"""资金漂移计算(V11.2 P0-3)

精确定义 equity / position / cash 三向漂移, 让 `FundCircuitBreaker` 的输入可信、可审计。
解决两个关键风险:

1. **drift 定义不清**: 明确每个维度的 numerator / denominator / 零基行为。
2. **missing-data 当 0 drift**: 交易所真相不完整或输入缺失时, 该维漂移记为 `None`(不可信),
   绝不静默当作「0 漂移」放行; 由上层按 DEGRADED / RECOVERY 处置。

漂移语义(统一约定, drift_pct 恒 >= 0, 0 = 无漂移):

    | 维度    | numerator                | denominator(明确)                      | 零基行为              |
    |---------|--------------------------|----------------------------------------|-----------------------|
    | equity  | |local - exchange|       | local_equity(本地权威记账)             | local<=0 → 不可计算   |
    | position| |local - exchange|       | max(|local|, |exchange|)(对称)         | 双边<=0 → 0 漂移      |
    | cash    | |local - exchange|       | max(|local|, |exchange|)(对称)         | 双边<=0 → 0 漂移      |

- 单边有仓位/现金(一边 0 一边 >0)→ 漂移 = 1.0(完整失配), 不会因分母取 0 而漏判。
- 交易所真相不完整(`truth_incomplete` / `pagination_exhausted`)→ 整体 `trusted=False`,
  所有维漂移置 `None`, 上层禁据此算「可信 drift」。

纯函数无副作用, 便于独立测试与对账循环复用。
"""

from dataclasses import dataclass
from typing import Optional

_TOL = 1e-12


@dataclass
class DriftResult:
    """三向漂移计算结果。trusted=False 时所有漂移为 None, 不得用于开仓决策。"""

    trusted: bool
    equity_drift: Optional[float]
    position_drift: Optional[float]
    cash_drift: Optional[float]
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "trusted": self.trusted,
            "equity_drift": self.equity_drift,
            "position_drift": self.position_drift,
            "cash_drift": self.cash_drift,
            "reason": self.reason,
        }


def _untrusted(reason: str) -> DriftResult:
    return DriftResult(False, None, None, None, reason)


def symmetric_ratio(local: float, exchange: float) -> float:
    """对称分母漂移率 = |local - exchange| / max(|local|, |exchange|)。

    - 双边 <= 容差 → 0.0(无该维度持仓/现金, 不算漂移)。
    - 单边 > 0(另一边 0)→ 1.0(完整失配), 不会因分母取 0 而漏判。
    """
    diff = abs(local - exchange)
    base = max(abs(local), abs(exchange))
    if base <= _TOL:
        return 0.0
    return diff / base


def equity_ratio(local: float, exchange: float) -> Optional[float]:
    """权益漂移率 = |local - exchange| / local(本地权威记账为分母)。

    - local <= 0 → 无法对非正基准计百分比 → None(不可计算, 缺失)。
    """
    if local <= _TOL:
        return None
    return abs(local - exchange) / local


def compute_drift(
    local_equity: Optional[float] = None,
    exchange_equity: Optional[float] = None,
    local_position: Optional[float] = None,
    exchange_position: Optional[float] = None,
    local_cash: Optional[float] = None,
    exchange_cash: Optional[float] = None,
    truth_complete: bool = True,
    symbol: str = "",
) -> DriftResult:
    """三向漂移计算(纯函数)。

    任一必需输入缺失(None)/ 交易所真相不完整 → 该维(或整体)不可信。
    """
    if not truth_complete:
        return _untrusted("truth_incomplete")

    # equity: 需双边 + 本地为正
    if local_equity is None or exchange_equity is None:
        return _untrusted("missing equity")
    eq = equity_ratio(local_equity, exchange_equity)
    if eq is None:
        return _untrusted("local_equity <= 0")

    # position: 需双边
    if local_position is None or exchange_position is None:
        return _untrusted("missing position")
    pos = symmetric_ratio(local_position, exchange_position)

    # cash: 需双边
    if local_cash is None or exchange_cash is None:
        return _untrusted("missing cash")
    cash = symmetric_ratio(local_cash, exchange_cash)

    return DriftResult(True, eq, pos, cash, "")
