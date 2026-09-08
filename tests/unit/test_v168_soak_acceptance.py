"""V11.7 P0-3 证明: soak 验收契约(evaluate_soak_result → PASS / FAIL / BLOCKED)。

核心原则: 「can_buy 曾经 false」不等于失败(DEGRADED → can_buy=false 可能是正确安全行为);
是否违反「预期安全契约」才是判定依据。判定维度:
- BLOCKED: 无样本 / 未跑满请求时长 / 样本不足(环境或提前中止, 不足以判定);
- FAIL: 关键任务失败 / 意外急停(unexpected KILL)/ 最终对账未通过 / 运行期未处理异常 /
  进程非正常退出;
- PASS: 跑满请求时长 + 样本充足 + 无上述违反。

覆盖 `at01_common/soak.py::evaluate_soak_result` + `_classify_shutdown` 纯逻辑。
"""

from at01_common.soak import (
    _classify_shutdown,
    build_evidence_line,
    evaluate_soak_result,
)


def _health(**overrides) -> dict:
    h = {
        "state": "TRADING",
        "status": "TRADING",
        "can_buy": True,
        "can_sell": True,
        "kill_switch": {"armed": False},
        "reconcile": {"reconciled": True},
        "uptime_seconds": 120.5,
        "tasks": {"failure_count": 0},
        "buy_block_reason": "",
        "sell_block_reason": "",
        "last_error": "",
    }
    h.update(overrides)
    return h


def _lines(n: int, start: float = 1000.0, step: float = 60.0) -> list[dict]:
    return [build_evidence_line(start + i * step, _health()) for i in range(n)]


_GRACEFUL = [{"event": "graceful_exit", "exit": 0}]


# ---------------------------------------------------------------------------
# PASS
# ---------------------------------------------------------------------------


def test_pass_full_healthy_graceful():
    lines = _lines(12)  # 12 样本, 660s 跨度
    r = evaluate_soak_result(lines, requested_duration_s=600.0, shutdown_events=_GRACEFUL)
    assert r["result"] == "PASS"
    assert r["reason"] == []
    assert r["samples"] == 12
    assert r["duration_s"] == 660.0
    assert r["shutdown_method"] == "graceful"
    assert r["critical_failures"] == 0
    assert r["unexpected_kill"] is False


def test_can_buy_false_transient_is_not_failure():
    """DEGRADED → can_buy=false(中途)后恢复, 是正确安全行为, 非 FAIL。"""
    lines = _lines(12)
    lines[5] = build_evidence_line(lines[5]["ts"], _health(state="DEGRADED", can_buy=False))
    r = evaluate_soak_result(lines, requested_duration_s=600.0, shutdown_events=_GRACEFUL)
    assert r["result"] == "PASS"
    assert r["any_can_buy_false"] is True


# ---------------------------------------------------------------------------
# FAIL(违反安全契约)
# ---------------------------------------------------------------------------


def test_fail_critical_task_failure():
    lines = _lines(12)
    lines[-1] = build_evidence_line(lines[-1]["ts"], _health(tasks={"failure_count": 3}))
    r = evaluate_soak_result(lines, requested_duration_s=600.0, shutdown_events=_GRACEFUL)
    assert r["result"] == "FAIL"
    assert r["critical_failures"] == 3
    assert any("关键后台任务失败" in x for x in r["reason"])


def test_fail_unexpected_kill_via_kill_switch():
    lines = _lines(12)
    lines[-1] = build_evidence_line(lines[-1]["ts"], _health(kill_switch={"armed": True}))
    r = evaluate_soak_result(lines, requested_duration_s=600.0, shutdown_events=_GRACEFUL)
    assert r["result"] == "FAIL"
    assert r["unexpected_kill"] is True
    assert any("意外急停" in x for x in r["reason"])


def test_fail_unexpected_kill_via_final_state():
    lines = _lines(12)
    lines[-1] = build_evidence_line(lines[-1]["ts"], _health(state="KILLED"))
    r = evaluate_soak_result(lines, requested_duration_s=600.0, shutdown_events=_GRACEFUL)
    assert r["result"] == "FAIL"
    assert r["unexpected_kill"] is True


def test_fail_reconciliation_failure():
    lines = _lines(12)
    lines[-1] = build_evidence_line(lines[-1]["ts"], _health(reconcile={"reconciled": False}))
    r = evaluate_soak_result(lines, requested_duration_s=600.0, shutdown_events=_GRACEFUL)
    assert r["result"] == "FAIL"
    assert r["final_reconciled"] is False
    assert any("对账" in x for x in r["reason"])


def test_fail_runtime_exception():
    lines = _lines(12)
    lines[-1] = build_evidence_line(lines[-1]["ts"], _health(last_error="RuntimeError: boom"))
    r = evaluate_soak_result(lines, requested_duration_s=600.0, shutdown_events=_GRACEFUL)
    assert r["result"] == "FAIL"
    assert r["final_last_error"] is True
    assert any("运行时" in x or "异常" in x for x in r["reason"])


def test_fail_process_crashed():
    lines = _lines(12)
    r = evaluate_soak_result(
        lines, requested_duration_s=600.0, shutdown_events=[{"event": "already_exited", "exit": 1}]
    )
    assert r["result"] == "FAIL"
    assert r["shutdown_method"] == "crashed"
    assert any("进程非正常退出" in x for x in r["reason"])


# ---------------------------------------------------------------------------
# BLOCKED(不足以判定)
# ---------------------------------------------------------------------------


def test_blocked_no_samples():
    r = evaluate_soak_result([], requested_duration_s=600.0)
    assert r["result"] == "BLOCKED"
    assert any("无样本" in x for x in r["reason"])


def test_blocked_duration_too_short():
    lines = _lines(12)  # 660s, 但请求 100000s → 未跑满
    r = evaluate_soak_result(lines, requested_duration_s=100000.0, shutdown_events=_GRACEFUL)
    assert r["result"] == "BLOCKED"
    assert any("实际时长" in x for x in r["reason"])


def test_blocked_insufficient_samples():
    lines = _lines(3, step=600.0)  # 3 样本(时长够), 但样本数不足
    r = evaluate_soak_result(lines, requested_duration_s=600.0, min_samples=10)
    assert r["result"] == "BLOCKED"
    assert any("样本数" in x for x in r["reason"])


# ---------------------------------------------------------------------------
# 结果结构与 _classify_shutdown
# ---------------------------------------------------------------------------


def test_result_has_spec_fields():
    r = evaluate_soak_result(_lines(12), requested_duration_s=600.0, shutdown_events=_GRACEFUL)
    for key in ("result", "reason", "duration_s", "samples", "critical_failures",
                "unexpected_kill", "final_state", "final_reconciled",
                "any_can_buy_false", "final_last_error", "shutdown_method"):
        assert key in r, f"缺少字段 {key}"


def test_classify_shutdown_variants():
    assert _classify_shutdown([]) == ("not_launched", False)
    assert _classify_shutdown([{"event": "terminated_exit", "exit": -15}]) == ("terminated", False)
    assert _classify_shutdown([{"event": "killed_exit", "exit": -9}]) == ("killed", False)
    assert _classify_shutdown([{"event": "already_exited", "exit": 0}]) == ("self_exited", False)
    assert _classify_shutdown([{"event": "already_exited", "exit": 1}]) == ("crashed", True)
    assert _classify_shutdown([{"event": "kill_timeout", "seconds": 15}]) == ("kill_timeout", True)
