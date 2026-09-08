"""对账矩阵(V11.1 P0-5)

统一各对账器(Position / Cross / ExchangeTruth / Lot 总和 / Equity / Recovery)的差异处置,
收敛为单一判定点, 消除「各对账器分散、各自独立 arm kill」的现状。

判定档位(四态):
- PASS: 无(或仅可观测性)差异。
- DEGRADED: 降级 —— 暂停交易, 冷却后自动恢复(数据不完整 / 持仓数量漂移 / 现金异常)。
- RECOVERY_REQUIRED: 需恢复 —— 暂停交易并等待自愈(订单恢复未收敛 / 单一内部对账器不一致),
  不持久急停。
- KILLED: 急停冻结 —— 持久化, 需人工 RECOVERY_CHECK 解除。

核心规则「单一对账器不得 kill」:
- 跨源资金级差异(本地 vs 交易所两套独立系统, 天然交叉验证)可单源即 kill:
  equity_drift / orphan_trade / exchange_only / fill_truth_missing / fill_truth_mismatch。
- 本地 DB 内部一致性破坏(fill/lot/sell_alloc/ledger 不互恰)单一对账器只降级为
  RECOVERY_REQUIRED(先自愈, 不 kill); 仅当 ≥2 个独立对账器在同一周期共同报告
  内部不一致(佐证)时才升级为 KILLED。
- 可观测性信号(api_error / trade_duplicate / trade_id_gap 等)仅记录, 不驱动处置。
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from at01_common.logger import LoggerMixin


class Severity(str, Enum):
    PASS = "PASS"
    DEGRADED = "DEGRADED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    KILLED = "KILLED"


# 跨源资金级差异: 单源即 kill(本地 vs 交易所, 天然交叉验证)
_KILLED_ALONE = frozenset({
    "equity_drift",
    "orphan_trade",
    "exchange_only",
    "fill_truth_missing",
    "fill_truth_mismatch",
})
# 本地 DB 内部一致性破坏: 单一对账器只 RECOVERY_REQUIRED, 需第二独立对账器佐证才 kill
_KILLED_CORROBORATED = frozenset({
    "fill_missing",
    "fill_mismatch",
    "fill_side_mismatch",
    "ledger_missing",
    "ledger_position_mismatch",
    "buy_lot_mismatch",
    "sell_alloc_mismatch",
    "lot_sum_mismatch",
})
# 需恢复(不 kill)
_RECOVERY_REQUIRED = frozenset({"recover_unresolved"})
# 降级(可自动恢复)
_DEGRADED = frozenset({
    "pagination_exhausted",
    "truth_incomplete",
    "paper_cash_negative",
    "mismatch",
})
# 可观测性信号(仅记录, 不驱动处置)
_OBSERVE = frozenset({
    "api_error",
    "cross_reconcile_error",
    "recover_error",
    "trade_duplicate",
    "trade_id_gap",
})


def classify_severity(type_: str) -> Severity:
    """差异类型 → 处置档位。未知类型保守降级(不静默、也不冒进 kill)。"""
    if type_ in _KILLED_ALONE or type_ in _KILLED_CORROBORATED:
        return Severity.KILLED
    if type_ in _RECOVERY_REQUIRED:
        return Severity.RECOVERY_REQUIRED
    if type_ in _DEGRADED:
        return Severity.DEGRADED
    if type_ in _OBSERVE:
        return Severity.PASS
    return Severity.DEGRADED


def _requires_corroboration(type_: str) -> bool:
    """该 KILLED 档差异是否需第二独立对账器佐证才能 kill"""
    return type_ in _KILLED_CORROBORATED


@dataclass
class Finding:
    """一条对账差异(带来源对账器标签)"""

    reconciler: str
    type: str
    symbol: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def severity(self) -> Severity:
        return classify_severity(self.type)


@dataclass
class Verdict:
    """对账矩阵判定结果"""

    severity: Severity
    findings: list[Finding] = field(default_factory=list)  # 含可观测性差异(供日志)
    reasons: list[str] = field(default_factory=list)  # 可处置差异(非 PASS)

    @property
    def actionable(self) -> bool:
        return self.severity is not Severity.PASS


def _reason(f: Finding) -> str:
    cid = f.data.get("client_order_id") or f.data.get("exchange_order_id") or ""
    reason = f"{f.reconciler}:{f.type} {f.symbol}".strip()
    return f"{reason} {cid}".strip() if cid else reason


def aggregate(findings: list[Finding]) -> Verdict:
    """汇总差异为单一判定。

    规则:
    - 无可处置差异 → PASS;
    - 有 KILLED 档差异: 跨源单源即 kill, 或 ≥2 个独立对账器佐证 → KILLED;
      否则(单一内部对账器)→ RECOVERY_REQUIRED(先自愈, 不 kill);
    - 有 RECOVERY_REQUIRED 档 → RECOVERY_REQUIRED;
    - 其余 → DEGRADED。
    """
    actionable = [f for f in findings if f.severity is not Severity.PASS]
    if not actionable:
        return Verdict(Severity.PASS, list(findings), [])

    killer = [f for f in actionable if f.severity is Severity.KILLED]
    if killer:
        alone = any(not _requires_corroboration(f.type) for f in killer)
        corroborated = len({f.reconciler for f in killer}) >= 2
        if alone or corroborated:
            return Verdict(Severity.KILLED, list(findings), [_reason(f) for f in killer])
        return Verdict(
            Severity.RECOVERY_REQUIRED, list(findings), [_reason(f) for f in killer]
        )

    recovery = [f for f in actionable if f.severity is Severity.RECOVERY_REQUIRED]
    if recovery:
        return Verdict(Severity.RECOVERY_REQUIRED, list(findings), [_reason(f) for f in recovery])

    return Verdict(Severity.DEGRADED, list(findings), [_reason(f) for f in actionable])


class ReconciliationMatrix(LoggerMixin):
    """周期对账统一处置矩阵: 收集差异 → 汇总判定 → 单一 kill 决策点。"""

    def __init__(self):
        self._findings: list[Finding] = []

    def ingest(self, reconciler: str, findings: list[dict[str, Any]]) -> None:
        """摄入某对账器的一批原始差异 dict(空列表安全)。"""
        for f in findings or []:
            self._findings.append(Finding(
                reconciler=reconciler,
                type=str(f.get("type", "")),
                symbol=str(f.get("symbol", "")),
                data=f,
            ))

    def verdict(self) -> Verdict:
        return aggregate(self._findings)

    def reset(self) -> None:
        self._findings.clear()

    @property
    def findings(self) -> list[Finding]:
        return list(self._findings)
