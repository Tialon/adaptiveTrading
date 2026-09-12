"""V11.1 P1-4 生产可观测性单元测试。

覆盖: 订单失败率 / 百分位 / MetricsStore 采集与派生 / 阈值告警 / 策略归因。
"""

import pytest

from at60_execution.observability import (
    AlertThresholds,
    MetricsStore,
    evaluate_alerts,
    order_failure_rate,
    percentile_rank,
    strategy_attribution,
)


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------

def test_order_failure_rate():
    assert order_failure_rate(100, 5) == pytest.approx(0.05)
    assert order_failure_rate(0, 0) == 0.0  # 0 订单不除零
    assert order_failure_rate(10, 0) == 0.0


def test_percentile_rank():
    assert percentile_rank([], 95) == 0.0
    # nearest-rank: 10 个样本, P95 → 第 10 个(最大)
    samples = list(range(1, 11))
    assert percentile_rank(samples, 95) == 10.0
    assert percentile_rank(samples, 50) == 5.0  # ceil(5.0)=5 → 第 5 个
    assert percentile_rank([5.0], 95) == 5.0


# ---------------------------------------------------------------------------
# MetricsStore
# ---------------------------------------------------------------------------

def test_store_counter_and_gauge():
    s = MetricsStore()
    s.incr("orders_total", 3)
    s.incr("orders_total")
    assert s.get_counter("orders_total") == 4
    s.gauge("reconcile_drift_pct", 0.03)
    assert s.get_gauge("reconcile_drift_pct") == pytest.approx(0.03)


def test_store_order_failure_rate():
    s = MetricsStore()
    s.incr("orders_total", 100)
    s.incr("orders_failed", 7)
    assert s.order_failure_rate() == pytest.approx(0.07)


def test_store_observe_and_percentile():
    s = MetricsStore()
    for v in (10, 20, 30, 40, 50):
        s.observe("execution_latency_ms", v)
    assert s.percentile("execution_latency_ms", 95) == 50.0
    assert s.percentile("execution_latency_ms", 50) == 30.0


def test_store_strategy_pnl_accumulates():
    s = MetricsStore()
    s.add_strategy_pnl("trend", 100.0)
    s.add_strategy_pnl("trend", -30.0)
    s.add_strategy_pnl("grid", 50.0)
    assert s.strategy_pnl == {"trend": 70.0, "grid": 50.0}


def test_snapshot_includes_derived():
    s = MetricsStore()
    s.incr("orders_total", 10)
    s.incr("orders_failed", 1)
    snap = s.snapshot()
    assert snap["order_failure_rate"] == pytest.approx(0.1)
    assert snap["recoveries"] == 0


# ---------------------------------------------------------------------------
# evaluate_alerts
# ---------------------------------------------------------------------------

def test_no_alerts_when_within_thresholds():
    s = MetricsStore()
    s.incr("orders_total", 100)
    s.incr("orders_failed", 1)  # 1%
    s.gauge("reconcile_drift_pct", 0.01)
    s.gauge("data_gap_seconds", 60.0)
    s.observe("execution_latency_ms", 100.0)
    assert evaluate_alerts(s) == []


def test_order_failure_rate_alert():
    s = MetricsStore()
    s.incr("orders_total", 100)
    s.incr("orders_failed", 10)  # 10% > 5%
    alerts = evaluate_alerts(s)
    assert any(a.name == "order_failure_rate" and a.severity == "CRITICAL" for a in alerts)


def test_reconcile_drift_alert():
    s = MetricsStore()
    s.gauge("reconcile_drift_pct", 0.05)  # 5% > 2%
    alerts = evaluate_alerts(s)
    assert any(a.name == "reconcile_drift" and a.severity == "WARNING" for a in alerts)


def test_data_gap_alert():
    s = MetricsStore()
    s.gauge("data_gap_seconds", 600.0)  # > 300s
    assert any(a.name == "data_gap" for a in evaluate_alerts(s))


def test_latency_p95_alert():
    s = MetricsStore()
    for v in (100, 200, 300, 400, 9000):  # P95 = 9000 > 5000
        s.observe("execution_latency_ms", v)
    assert any(a.name == "execution_latency_p95" for a in evaluate_alerts(s))


def test_recovery_streak_alert():
    s = MetricsStore()
    s.incr("recovery_streak", 5)  # >= 5
    assert any(a.name == "recovery_streak" for a in evaluate_alerts(s))


def test_multiple_alerts_aggregated():
    s = MetricsStore()
    s.incr("orders_total", 100)
    s.incr("orders_failed", 20)
    s.gauge("data_gap_seconds", 999.0)
    alerts = evaluate_alerts(s)
    names = {a.name for a in alerts}
    assert {"order_failure_rate", "data_gap"} <= names


def test_custom_thresholds():
    s = MetricsStore()
    s.gauge("reconcile_drift_pct", 0.03)
    # 默认 2% → 告警; 放宽到 10% → 无告警
    assert any(a.name == "reconcile_drift" for a in evaluate_alerts(s))
    t = AlertThresholds(reconcile_drift_pct=0.10)
    assert evaluate_alerts(s, t) == []


# ---------------------------------------------------------------------------
# strategy_attribution
# ---------------------------------------------------------------------------

def test_strategy_attribution_sorted_and_share():
    s = MetricsStore()
    s.add_strategy_pnl("grid", 50.0)
    s.add_strategy_pnl("trend", 150.0)
    rows = strategy_attribution(s)
    assert rows[0]["strategy"] == "trend"  # 降序
    assert rows[0]["pnl"] == 150.0
    assert rows[0]["share"] == pytest.approx(0.75)
    assert rows[1]["share"] == pytest.approx(0.25)


def test_strategy_attribution_empty():
    assert strategy_attribution(MetricsStore()) == []
