"""V11.2 P1-1 / P1-2 真实运行生命周期 + 可观测性接入。

P1-1: 顶层生命周期状态机在运行期由对账/熔断真正驱动:
  - `SystemLifecycle` 迁移带 timestamp + from/to/reason 审计轨迹(禁止非法跳转);
  - `apply_reconcile_verdict`: 对账判定 -> 生命周期迁移(DEGRADED/RECOVERY/SAFE_MODE, PASS 自动恢复交易)。

P1-2: 生产可观测性真正被喂入:
  - `record_execution`: 下单尝试 -> orders_total/failed/unknown/recovery_required + 延迟采样 + 策略归因;
  - `record_reconcile_verdict` / `record_breaker_action`: 对账判定 / 熔断动作 -> 计数器;
  - `evaluate_alerts` 能基于真实采集值告警(已由 test_v119 覆盖阈值, 此处验接线语义)。
"""

import time

import pytest

from at50_execution.observability import (
    MetricsStore,
    record_breaker_action,
    record_execution,
    record_reconcile_verdict,
)
from at60_risk.fund_circuit_breaker import FundCircuitBreaker
from at60_risk.risk_manager import RiskManager
from at60_risk.system_lifecycle import (
    LifecycleState,
    SystemLifecycle,
    apply_reconcile_verdict,
)
from at60_risk.trading_gate import TradingGate


def _go_trading() -> SystemLifecycle:
    lc = SystemLifecycle()
    assert lc.warm_up()
    assert lc.sync()
    assert lc.self_check()
    assert lc.ready()
    assert lc.start_trading()
    return lc


# ---------------------------------------------------------------------------
# P1-1a: 迁移审计轨迹(timestamp / from / to / reason)
# ---------------------------------------------------------------------------

def test_transition_history_recorded():
    lc = _go_trading()
    assert len(lc.history) == 5  # warm_up/sync/self_check/ready/start_trading
    assert lc.last_transition_at is not None
    for h in lc.history:
        assert set(h) == {"ts", "from", "to", "reason"}
        assert h["from"] != h["to"]
    # 轨迹有序, 终态为 TRADING
    assert lc.history[-1]["to"] == LifecycleState.TRADING.value
    assert lc.history[0]["from"] == LifecycleState.INIT.value


def test_illegal_transition_not_recorded():
    lc = SystemLifecycle()
    assert lc.start_trading() is False  # INIT -> TRADING 非法
    assert lc.history == []  # 非法迁移不落轨迹
    assert lc.last_transition_at is None


def test_history_is_copy_not_internal_ref():
    lc = _go_trading()
    snap = lc.history
    snap.append({"ts": 0.0, "from": "X", "to": "Y", "reason": ""})
    assert len(lc.history) == 5  # 内部轨迹不受外部篡改影响


# ---------------------------------------------------------------------------
# P1-1b: apply_reconcile_verdict(对账判定 -> 生命周期)
# ---------------------------------------------------------------------------

def test_verdict_degraded():
    lc = _go_trading()
    assert apply_reconcile_verdict(lc, "DEGRADED", "权益漂移") == "DEGRADED"
    assert lc.state is LifecycleState.DEGRADED


def test_verdict_recovery_required():
    lc = _go_trading()
    assert apply_reconcile_verdict(lc, "RECOVERY_REQUIRED", "DB 失败") == "RECOVERY"
    assert lc.state is LifecycleState.RECOVERY


def test_verdict_killed_enters_safe_mode():
    lc = _go_trading()
    assert apply_reconcile_verdict(lc, "KILLED", "权益漂移") == "SAFE_MODE"
    assert lc.state is LifecycleState.SAFE_MODE


def test_verdict_pass_recovers_to_trading():
    lc = _go_trading()
    apply_reconcile_verdict(lc, "DEGRADED", "ws 断开")
    assert apply_reconcile_verdict(lc, "PASS") == "TRADING"
    assert lc.state is LifecycleState.TRADING


def test_verdict_pass_from_recovery_recovers():
    lc = _go_trading()
    apply_reconcile_verdict(lc, "RECOVERY_REQUIRED", "对账需恢复")
    assert lc.state is LifecycleState.RECOVERY
    assert apply_reconcile_verdict(lc, "PASS") == "TRADING"


def test_verdict_pass_does_not_exit_safe_mode():
    # SAFE_MODE 需人工 exit_safe_mode, 对账 PASS 不得自动解除冻结
    lc = _go_trading()
    apply_reconcile_verdict(lc, "KILLED", "对账冲突")
    assert apply_reconcile_verdict(lc, "PASS") == "SAFE_MODE"
    assert lc.state is LifecycleState.SAFE_MODE


def test_verdict_pass_idempotent_when_trading():
    lc = _go_trading()
    assert apply_reconcile_verdict(lc, "PASS") == "TRADING"
    assert lc.state is LifecycleState.TRADING


# ---------------------------------------------------------------------------
# P1-1c: 端到端 —— 对账判定经生命周期驱动统一闸门(降级禁开、恢复可开)
# ---------------------------------------------------------------------------

def test_verdict_drives_gate_end_to_end():
    rm = RiskManager()
    lc = _go_trading()
    gate = TradingGate(rm, lc, FundCircuitBreaker())
    assert gate.can_open_position()[0]  # 初始可开

    apply_reconcile_verdict(lc, "DEGRADED", "ws 断开")
    assert not gate.can_open_position()[0]  # 降级后禁开
    # 减仓在降级期仍允许(安全离场)
    assert gate.can_reduce_position()[0]

    apply_reconcile_verdict(lc, "PASS")
    assert gate.can_open_position()[0]  # 恢复后可开


# ---------------------------------------------------------------------------
# P1-2a: record_execution(下单尝试 -> 指标)
# ---------------------------------------------------------------------------

def test_record_execution_filled():
    s = MetricsStore()
    record_execution(s, status="FILLED", latency_ms=120.0, strategy="trend", realized_pnl=0.0)
    assert s.get_counter("orders_total") == 1
    assert s.get_counter("orders_failed") == 0
    assert s.get_counter("orders_unknown") == 0
    assert s.percentile("execution_latency_ms", 95) == 120.0


def test_record_execution_unknown_counts_failed_and_unknown():
    s = MetricsStore()
    record_execution(s, status="UNKNOWN", latency_ms=200.0, strategy="trend")
    assert s.get_counter("orders_total") == 1
    assert s.get_counter("orders_failed") == 1  # UNKNOWN 计入失败率
    assert s.get_counter("orders_unknown") == 1


def test_record_execution_recovery_required():
    s = MetricsStore()
    record_execution(s, status="RECOVERY_REQUIRED", latency_ms=150.0, strategy="trend")
    assert s.get_counter("orders_failed") == 1
    assert s.get_counter("recovery_required") == 1


def test_record_execution_rejected_counts_failed():
    s = MetricsStore()
    record_execution(s, status="REJECTED", latency_ms=10.0, strategy="trend")
    assert s.get_counter("orders_total") == 1
    assert s.get_counter("orders_failed") == 1
    assert s.get_counter("orders_unknown") == 0


def test_record_execution_strategy_pnl_attribution():
    s = MetricsStore()
    record_execution(s, status="FILLED", latency_ms=10.0, strategy="trend", realized_pnl=42.0)
    record_execution(s, status="FILLED", latency_ms=11.0, strategy="trend", realized_pnl=-12.0)
    # UNKNOWN 不归因盈亏(未确认成交)
    record_execution(s, status="UNKNOWN", latency_ms=12.0, strategy="trend", realized_pnl=999.0)
    assert s.strategy_pnl == {"trend": 30.0}


def test_record_execution_failure_rate_feeds_alert():
    s = MetricsStore()
    for _ in range(100):
        record_execution(s, status="FILLED", latency_ms=5.0, strategy="trend")
    for _ in range(6):
        record_execution(s, status="UNKNOWN", latency_ms=5.0, strategy="trend")
    from at50_execution.observability import evaluate_alerts

    assert any(a.name == "order_failure_rate" for a in evaluate_alerts(s))


# ---------------------------------------------------------------------------
# P1-2b: record_reconcile_verdict / record_breaker_action
# ---------------------------------------------------------------------------

def test_record_reconcile_verdict_counters():
    s = MetricsStore()
    record_reconcile_verdict(s, "DEGRADED")
    record_reconcile_verdict(s, "RECOVERY_REQUIRED")
    record_reconcile_verdict(s, "KILLED")
    assert s.get_counter("reconcile_degraded") == 1
    assert s.get_counter("reconcile_recovery_required") == 1
    assert s.get_counter("reconcile_killed") == 1


def test_record_breaker_action_counters():
    s = MetricsStore()
    record_breaker_action(s, "REDUCE_ONLY")
    record_breaker_action(s, "PAUSE")
    record_breaker_action(s, "KILL")
    assert s.get_counter("breaker_reduce_only") == 1
    assert s.get_counter("breaker_pause") == 1
    assert s.get_counter("breaker_kill") == 1


def test_record_breaker_action_none_is_noop():
    s = MetricsStore()
    record_breaker_action(s, None)
    record_breaker_action(s, "")
    assert s.counters == {}


# ---------------------------------------------------------------------------
# P1-2c: 行情数据缺口指标(RiskManager.ws_silence_seconds)
# ---------------------------------------------------------------------------

def test_ws_silence_seconds_initial_zero():
    rm = RiskManager()
    assert rm.ws_silence_seconds == 0.0  # 未收到 tick


def test_ws_silence_seconds_after_tick():
    rm = RiskManager()
    rm._last_tick_time = time.time() - 100.0
    assert rm.ws_silence_seconds == pytest.approx(100.0, abs=1.0)
