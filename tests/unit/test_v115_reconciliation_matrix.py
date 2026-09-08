"""V11.1 P0-5 对账矩阵(ReconciliationMatrix)单元测试。

验证统一四态判定 PASS/DEGRADED/RECOVERY_REQUIRED/KILLED, 以及核心规则
「单一对账器不得 kill」(本地 DB 内部一致性破坏需 ≥2 独立对账器佐证才升级 KILLED)。
"""

import pytest

from at50_execution.reconciliation_matrix import (
    Finding,
    ReconciliationMatrix,
    Severity,
    aggregate,
    classify_severity,
)


def _f(reconciler: str, type_: str, **data) -> Finding:
    return Finding(reconciler=reconciler, type=type_, data=data)


# ---------------------------------------------------------------------------
# classify_severity 映射
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "type_,expected",
    [
        # 跨源资金级差异 → 单源即 KILLED
        ("equity_drift", Severity.KILLED),
        ("orphan_trade", Severity.KILLED),
        ("exchange_only", Severity.KILLED),
        ("fill_truth_missing", Severity.KILLED),
        ("fill_truth_mismatch", Severity.KILLED),
        # 本地 DB 内部一致性破坏 → 也映射 KILLED(但 aggregate 需佐证)
        ("fill_missing", Severity.KILLED),
        ("fill_mismatch", Severity.KILLED),
        ("fill_side_mismatch", Severity.KILLED),
        ("ledger_missing", Severity.KILLED),
        ("ledger_position_mismatch", Severity.KILLED),
        ("buy_lot_mismatch", Severity.KILLED),
        ("sell_alloc_mismatch", Severity.KILLED),
        ("lot_sum_mismatch", Severity.KILLED),
        # 需恢复
        ("recover_unresolved", Severity.RECOVERY_REQUIRED),
        # 降级
        ("pagination_exhausted", Severity.DEGRADED),
        ("truth_incomplete", Severity.DEGRADED),
        ("paper_cash_negative", Severity.DEGRADED),
        ("mismatch", Severity.DEGRADED),
        # 可观测性信号
        ("api_error", Severity.PASS),
        ("cross_reconcile_error", Severity.PASS),
        ("recover_error", Severity.PASS),
        ("trade_duplicate", Severity.PASS),
        ("trade_id_gap", Severity.PASS),
    ],
)
def test_classify_severity(type_, expected):
    assert classify_severity(type_) is expected


def test_unknown_type_conservatively_degrades():
    # 未知差异类型保守降级: 不静默、也不冒进 kill
    assert classify_severity("some_future_signal") is Severity.DEGRADED


# ---------------------------------------------------------------------------
# aggregate 判定
# ---------------------------------------------------------------------------

def test_empty_findings_pass():
    v = aggregate([])
    assert v.severity is Severity.PASS
    assert v.reasons == []
    assert not v.actionable


def test_observe_only_pass():
    findings = [
        _f("position", "api_error"),
        _f("exchange_truth", "trade_duplicate"),
        _f("exchange_truth", "trade_id_gap"),
    ]
    v = aggregate(findings)
    assert v.severity is Severity.PASS
    assert v.reasons == []
    # 可观测性差异仍保留供日志
    assert len(v.findings) == 3


def test_degraded_single_signal():
    v = aggregate([_f("exchange_truth", "truth_incomplete")])
    assert v.severity is Severity.DEGRADED


def test_recovery_required_recover_unresolved():
    v = aggregate([_f("recovery", "recover_unresolved", client_order_id="c1")])
    assert v.severity is Severity.RECOVERY_REQUIRED
    assert v.reasons == ["recovery:recover_unresolved c1"]


@pytest.mark.parametrize("type_", ["equity_drift", "orphan_trade", "exchange_only"])
def test_killed_alone_cross_source(type_):
    # 跨源资金级差异: 单一对账器即 KILLED(本地 vs 交易所天然交叉验证)
    v = aggregate([_f("position", type_, symbol="SOLUSDT")])
    assert v.severity is Severity.KILLED


def test_single_internal_finding_is_not_killed():
    # 本地 DB 内部一致性破坏, 单一对账器 → 只 RECOVERY_REQUIRED(先自愈, 不 kill)
    v = aggregate([_f("cross", "fill_missing", client_order_id="c1")])
    assert v.severity is Severity.RECOVERY_REQUIRED


def test_corroborated_two_reconcilers_kills():
    # 两个独立对账器同周期共同报告内部不一致 → 佐证成立 → KILLED
    findings = [
        _f("cross", "fill_missing", client_order_id="c1"),
        _f("exchange_truth", "fill_truth_mismatch", exchange_order_id="e1"),
    ]
    # 注: fill_truth_mismatch 本身即单源 kill; 这里换一组纯内部信号验证佐证
    findings2 = [
        _f("cross", "fill_missing", client_order_id="c1"),
        _f("lot", "lot_sum_mismatch", symbol="SOLUSDT"),
    ]
    v = aggregate(findings2)
    assert v.severity is Severity.KILLED
    # 前者(fill_truth_mismatch 跨源)同样 kill
    assert aggregate(findings).severity is Severity.KILLED


def test_same_reconciler_multiple_not_killed():
    # 同一对账器报多条内部不一致, 仍是单一来源 → 不 kill
    findings = [
        _f("cross", "fill_missing", client_order_id="c1"),
        _f("cross", "buy_lot_mismatch", client_order_id="c1"),
    ]
    v = aggregate(findings)
    assert v.severity is Severity.RECOVERY_REQUIRED


def test_killed_outranks_degraded():
    # KILLED 档差异存在时, 即便同时有降级信号, 判定仍为 KILLED
    findings = [
        _f("exchange_truth", "truth_incomplete"),
        _f("position", "equity_drift"),
    ]
    assert aggregate(findings).severity is Severity.KILLED


# ---------------------------------------------------------------------------
# ReconciliationMatrix 类
# ---------------------------------------------------------------------------

def test_matrix_ingest_and_verdict():
    m = ReconciliationMatrix()
    m.ingest("position", [{"type": "mismatch", "symbol": "SOLUSDT"}])
    m.ingest("cross", [])
    m.ingest("exchange_truth", None)  # None 安全
    v = m.verdict()
    assert v.severity is Severity.DEGRADED
    assert len(m.findings) == 1


def test_matrix_reset():
    m = ReconciliationMatrix()
    m.ingest("position", [{"type": "equity_drift"}])
    assert m.verdict().severity is Severity.KILLED
    m.reset()
    assert m.verdict().severity is Severity.PASS
    assert m.findings == []


def test_ingest_tags_reconciler():
    m = ReconciliationMatrix()
    m.ingest("lot", [{"type": "lot_sum_mismatch", "symbol": "SOLUSDT", "diff": 0.5}])
    (f,) = m.findings
    assert f.reconciler == "lot"
    assert f.type == "lot_sum_mismatch"
    assert f.symbol == "SOLUSDT"
    assert f.data["diff"] == 0.5
