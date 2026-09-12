"""V11.3 P0-10 可观测性加固(样本有界 + 恢复计数接线 + 告警可达)。

既有覆盖: test_v119(MetricsStore 采集 / 阈值告警 / 归因)。本片补「加固」级不变量:

1. **样本有界**: `observe` 超上界丢弃最旧, 长跑下 execution_latency_ms 不无界增长
   (否则每次告警评估/`/api/metrics` 对全量样本排序, 内存与 CPU 随时间线性恶化);
2. **恢复计数接线**: `record_reconcile_verdict` 维护 recoveries / recovery_streak,
   PASS 归零 —— 修复此前 recovery_streak 只读不写、`连续恢复次数` 告警永不可达的死指标;
3. **告警可达**: 连续 5 个非 PASS 周期后 recovery_streak 告警真实触发, PASS 后解除。

冻结不变: 纯可观测性加固, 不改交易/记账逻辑。
"""

import pytest

from at60_execution.observability import (
    MAX_SAMPLE_LEN,
    AlertThresholds,
    MetricsStore,
    evaluate_alerts,
    record_reconcile_verdict,
)


# ---------------------------------------------------------------------------
# 1. 样本有界
# ---------------------------------------------------------------------------

def test_observe_bounded_keeps_newest():
    s = MetricsStore()
    n = MAX_SAMPLE_LEN + 500
    for i in range(n):
        s.observe("execution_latency_ms", float(i))
    buf = s.samples["execution_latency_ms"]
    assert len(buf) == MAX_SAMPLE_LEN
    # 保留最新: 首元素 == 第 500 个(最旧 MAX_SAMPLE_LEN 之前的被丢弃)
    assert buf[0] == pytest.approx(500.0)
    assert buf[-1] == pytest.approx(float(n - 1))


def test_observe_multiple_series_each_bounded():
    s = MetricsStore()
    for i in range(MAX_SAMPLE_LEN + 10):
        s.observe("a", float(i))
        s.observe("b", float(i))
    assert len(s.samples["a"]) == MAX_SAMPLE_LEN
    assert len(s.samples["b"]) == MAX_SAMPLE_LEN


def test_set_counter():
    s = MetricsStore()
    s.incr("recovery_streak", 3)
    assert s.get_counter("recovery_streak") == 3
    s.set_counter("recovery_streak", 0)
    assert s.get_counter("recovery_streak") == 0


# ---------------------------------------------------------------------------
# 2. 恢复计数接线
# ---------------------------------------------------------------------------

def test_reconcile_verdict_wires_recovery_counters():
    s = MetricsStore()
    record_reconcile_verdict(s, "RECOVERY_REQUIRED")
    assert s.get_counter("recoveries") == 1
    assert s.get_counter("recovery_streak") == 1
    assert s.get_counter("reconcile_recovery_required") == 1

    record_reconcile_verdict(s, "DEGRADED")
    assert s.get_counter("recoveries") == 2
    assert s.get_counter("recovery_streak") == 2
    assert s.get_counter("reconcile_degraded") == 1

    record_reconcile_verdict(s, "KILLED")
    assert s.get_counter("recoveries") == 3
    assert s.get_counter("recovery_streak") == 3
    assert s.get_counter("reconcile_killed") == 1


def test_pass_resets_streak_but_keeps_total():
    s = MetricsStore()
    for _ in range(3):
        record_reconcile_verdict(s, "RECOVERY_REQUIRED")
    assert s.get_counter("recoveries") == 3
    assert s.get_counter("recovery_streak") == 3

    record_reconcile_verdict(s, "PASS")
    assert s.get_counter("recovery_streak") == 0  # 连续计数归零
    assert s.get_counter("recoveries") == 3      # 累计保留


# ---------------------------------------------------------------------------
# 3. 告警可达(此前死指标修复后)
# ---------------------------------------------------------------------------

def test_recovery_streak_alert_reachable():
    s = MetricsStore()
    # 连续 5 个非 PASS 周期 -> recovery_streak = 5 -> 告警触发
    for _ in range(5):
        record_reconcile_verdict(s, "RECOVERY_REQUIRED")
    alerts = evaluate_alerts(s)
    assert any(a.name == "recovery_streak" for a in alerts)

    # PASS 归零后告警解除
    record_reconcile_verdict(s, "PASS")
    assert all(a.name != "recovery_streak" for a in evaluate_alerts(s))


def test_recovery_streak_below_threshold_no_alert():
    s = MetricsStore()
    for _ in range(4):  # 4 < 5
        record_reconcile_verdict(s, "DEGRADED")
    assert all(a.name != "recovery_streak" for a in evaluate_alerts(s))


def test_custom_recovery_threshold():
    s = MetricsStore()
    for _ in range(2):
        record_reconcile_verdict(s, "RECOVERY_REQUIRED")
    # 默认 5 -> 无告警; 收紧到 2 -> 告警
    assert all(a.name != "recovery_streak" for a in evaluate_alerts(s))
    t = AlertThresholds(recovery_streak=2)
    assert any(a.name == "recovery_streak" for a in evaluate_alerts(s, t))
