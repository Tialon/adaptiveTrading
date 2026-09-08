"""V11.6 P1-7 / P1-8 证明: soak 运行时证据记录的纯逻辑(证据行 / 迁移检测 / 摘要)。

覆盖 `at01_common/soak.py` 的三段纯函数:
1. `build_evidence_line`: 从 /api/metrics 健康快照(P1-2 契约)提取稳定可审计字段;
   字段缺失时保守回退, 不因面板字段漂移崩溃;
2. `classify_soak_event`: 检测值得记录的迁移(start / 状态变化 / 禁买翻转 / 急停触发);
   无变化返回 None;
3. `summarize_soak`: 证据摘要(样本数 / 时长 / 状态分布 / 曾禁买·急停 / 峰值失败)。

运维主循环(子进程启动 + urllib 轮询)依赖真实 run.py + 测试网, 不在此单元测试;
它由 testnet-runbook.md §4-8 的操作流程覆盖。
"""

from at01_common.soak import build_evidence_line, classify_soak_event, summarize_soak


def _health(**overrides) -> dict:
    """构造 P1-2 契约形状的健康快照, 允许字段级覆盖。"""
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
    }
    h.update(overrides)
    return h


# ---------------------------------------------------------------------------
# build_evidence_line
# ---------------------------------------------------------------------------


def test_build_evidence_line_extracts_fields():
    line = build_evidence_line(1000.0, _health())
    assert line["ts"] == 1000.0
    assert line["state"] == "TRADING"
    assert line["can_buy"] is True
    assert line["can_sell"] is True
    assert line["kill_armed"] is False
    assert line["reconciled"] is True
    assert line["uptime_s"] == 120.5
    assert line["tasks_failure_count"] == 0
    assert line["buy_block_reason"] == ""


def test_build_evidence_line_conservative_on_missing():
    line = build_evidence_line(1.0, {})
    assert line["state"] == "?"
    assert line["can_buy"] is False
    assert line["can_sell"] is False
    assert line["kill_armed"] is False
    assert line["reconciled"] is False
    assert line["uptime_s"] == 0.0
    assert line["tasks_failure_count"] == 0
    assert line["buy_block_reason"] == ""


def test_build_evidence_line_state_falls_back_to_status():
    line = build_evidence_line(1.0, {"status": "SAFE"})
    assert line["state"] == "SAFE"


# ---------------------------------------------------------------------------
# classify_soak_event(作用于 build_evidence_line 产出的证据行, 而非原始 health)
# ---------------------------------------------------------------------------


def _line(ts: float, **overrides) -> dict:
    return build_evidence_line(ts, _health(**overrides))


def test_classify_start():
    assert classify_soak_event(None, _line(1.0)) == "start"


def test_classify_state_change():
    prev = _line(1.0)
    cur = _line(2.0, state="KILLED")
    ev = classify_soak_event(prev, cur)
    assert ev is not None and "TRADING->KILLED" in ev


def test_classify_can_buy_flip_to_blocked():
    prev = _line(1.0)
    cur = _line(2.0, can_buy=False, buy_block_reason="急停")
    ev = classify_soak_event(prev, cur)
    assert ev is not None and "禁买" in ev and "急停" in ev


def test_classify_can_buy_recover():
    prev = _line(1.0, can_buy=False)
    cur = _line(2.0, can_buy=True)
    ev = classify_soak_event(prev, cur)
    assert ev is not None and "恢复" in ev


def test_classify_kill_trigger():
    prev = _line(1.0)
    cur = _line(2.0, kill_switch={"armed": True})
    ev = classify_soak_event(prev, cur)
    assert ev is not None and "急停触发" in ev


def test_classify_no_change_returns_none():
    assert classify_soak_event(_line(1.0), _line(2.0)) is None


# ---------------------------------------------------------------------------
# summarize_soak
# ---------------------------------------------------------------------------


def test_summarize_empty():
    assert summarize_soak([]) == {"samples": 0}


def test_summarize_stats():
    lines = [
        build_evidence_line(1000.0, _health()),
        build_evidence_line(1060.0, _health(state="PAUSED", can_buy=False)),
        build_evidence_line(
            1120.0,
            _health(state="TRADING", kill_switch={"armed": True}, tasks={"failure_count": 2}),
        ),
    ]
    s = summarize_soak(lines)
    assert s["samples"] == 3
    assert s["duration_s"] == 120.0
    assert s["states"] == {"TRADING": 2, "PAUSED": 1}
    assert s["any_can_buy_false"] is True
    assert s["any_kill_armed"] is True
    assert s["max_tasks_failure_count"] == 2
