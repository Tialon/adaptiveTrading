"""V11.4 P1-5 Observability 最终检查 —— 消除死指标。

审计发现两类死指标(采集与观测脱节):
1. 「写而不读」: `orders_unknown` / `recovery_required` / `reconcile_{degraded,recovery_required,killed}`
   / `breaker_{reduce_only,pause,kill}` 被 `record_*` 采集, 但从不进 `snapshot()` —— 面板/告警看不到。
   → 修复: 全部纳入 `MetricsStore.snapshot()`。
2. 「读而不写」: `data_gaps` 进了 snapshot 却从不 +1(恒为 0)。
   → 修复: run.py 在行情静默「进入」的瞬间 +1(经 `RiskManager.silence_active` 上升沿)。

本测试锁定这两处修复, 防止死指标回归。
"""

import time

from at60_execution.observability import (
    MetricsStore,
    record_breaker_action,
    record_execution,
    record_reconcile_verdict,
)
from at50_risk.risk_manager import RiskManager


class TestSnapshotNoDeadWrites:
    def test_snapshot_includes_all_collected_counters(self):
        s = MetricsStore()
        record_execution(s, status="UNKNOWN", latency_ms=10.0)
        record_execution(s, status="RECOVERY_REQUIRED", latency_ms=20.0)
        record_reconcile_verdict(s, "DEGRADED")
        record_reconcile_verdict(s, "RECOVERY_REQUIRED")
        record_reconcile_verdict(s, "KILLED")
        record_breaker_action(s, "REDUCE_ONLY")
        record_breaker_action(s, "PAUSE")
        record_breaker_action(s, "KILL")
        snap = s.snapshot()

        # 写了的计数器必须在 snapshot 里可见(否则是死写)
        assert snap["orders_unknown"] == 1
        assert snap["recovery_required"] == 1
        assert snap["reconcile_degraded"] == 1
        assert snap["reconcile_recovery_required"] == 1
        assert snap["reconcile_killed"] == 1
        assert snap["breaker_reduce_only"] == 1
        assert snap["breaker_pause"] == 1
        assert snap["breaker_kill"] == 1
        # data_gaps 也必须在(读而不写由 run.py 接线解决)
        assert "data_gaps" in snap


class TestSilenceActiveTransition:
    """RiskManager.silence_active 提供 data_gaps 计数所需的「进入静默」上升沿。"""

    def test_silence_active_initial_false(self):
        rm = RiskManager()
        assert rm.silence_active is False

    def test_silence_active_becomes_true_on_stale_tick(self):
        rm = RiskManager()
        rm._last_tick_time = time.time() - 100.0  # 静默 100s > 默认阈值 30s
        rm.check_market_silence()
        assert rm.silence_active is True

    def test_silence_active_resets_on_recovery(self):
        rm = RiskManager()
        rm._last_tick_time = time.time() - 100.0
        rm.check_market_silence()
        assert rm.silence_active is True
        # 行情恢复: 新 tick 到达 → 静默标记复位(下一次静默再 +1)
        rm._last_tick_time = time.time()
        rm.check_market_silence()
        assert rm.silence_active is False
