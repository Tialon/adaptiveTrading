"""V11.5 P1-1 运行时健康快照 + 单一状态分类 单元测试。

覆盖:
1. `classify_runtime_status` 纯函数: 七态(SAFE/DEGRADED/REDUCE_ONLY/PAUSED/
   RECOVERY/KILLED/TRADING)逐一判定, 以及 KILLED 的最高优先级(压过其它态);
2. `build_runtime_health` 对未初始化 SystemState 容错(返回「未运行」快照, 不抛);
3. `build_runtime_health` 聚合完整健康态(生命周期/风险/急停/熔断/任务/交易/市场/
   对账/最近事件/最近错误), 状态归并为 TRADING;
4. 急停 armed 经快照正确归类为 KILLED;
5. 任务聚合(total/running/failed/failure_count/active) 正确。
"""

import time
from types import SimpleNamespace

import pytest

from at01_common.runtime_health import (
    STATUS_DEGRADED,
    STATUS_KILLED,
    STATUS_PAUSED,
    STATUS_RECOVERY,
    STATUS_REDUCE_ONLY,
    STATUS_SAFE,
    STATUS_TRADING,
    build_runtime_health,
    classify_runtime_status,
)
from at10_web.web_state import SystemState


def _health(**overrides):
    """构造最小健康快照(健康=TRADING), 按需覆盖。"""
    base = {
        "kill_switch": {"armed": False},
        "lifecycle": {"state": "TRADING"},
        "risk": {"state": "NORMAL"},
        "breaker": {"open": False, "fund_action": "NONE"},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 1. 纯分类器
# ---------------------------------------------------------------------------


class TestClassifyStatus:
    def test_trading_when_healthy(self):
        assert classify_runtime_status(_health()) == STATUS_TRADING

    def test_safe_when_ready(self):
        assert classify_runtime_status(_health(lifecycle={"state": "READY"})) == STATUS_SAFE
        assert classify_runtime_status(_health(lifecycle={"state": "INIT"})) == STATUS_SAFE

    def test_degraded(self):
        assert classify_runtime_status(_health(lifecycle={"state": "DEGRADED"})) == STATUS_DEGRADED

    def test_reduce_only_risk_or_fund(self):
        assert classify_runtime_status(_health(risk={"state": "REDUCE_ONLY"})) == STATUS_REDUCE_ONLY
        assert (
            classify_runtime_status(_health(breaker={"open": False, "fund_action": "REDUCE_ONLY"}))
            == STATUS_REDUCE_ONLY
        )

    def test_paused_risk_breaker_fund(self):
        assert classify_runtime_status(_health(risk={"state": "PAUSED"})) == STATUS_PAUSED
        assert (
            classify_runtime_status(_health(breaker={"open": True, "fund_action": "NONE"}))
            == STATUS_PAUSED
        )
        assert (
            classify_runtime_status(_health(breaker={"open": False, "fund_action": "PAUSE"}))
            == STATUS_PAUSED
        )

    def test_recovery_check_or_lifecycle(self):
        assert classify_runtime_status(_health(risk={"state": "RECOVERY_CHECK"})) == STATUS_RECOVERY
        assert classify_runtime_status(_health(lifecycle={"state": "RECOVERY"})) == STATUS_RECOVERY

    def test_killed_via_kill_switch(self):
        assert classify_runtime_status(_health(kill_switch={"armed": True})) == STATUS_KILLED

    def test_killed_via_safe_mode(self):
        assert classify_runtime_status(_health(lifecycle={"state": "SAFE_MODE"})) == STATUS_KILLED

    def test_killed_via_risk_killed(self):
        assert classify_runtime_status(_health(risk={"state": "KILLED"})) == STATUS_KILLED

    def test_killed_priority_overrides_paused_and_reduce_only(self):
        # 冻结优先级最高: 即使同时 PAUSED / REDUCE_ONLY / RECOVERY, 仍判 KILLED
        h = _health(
            kill_switch={"armed": True},
            lifecycle={"state": "RECOVERY"},
            risk={"state": "PAUSED"},
            breaker={"open": True, "fund_action": "KILL"},
        )
        assert classify_runtime_status(h) == STATUS_KILLED


# ---------------------------------------------------------------------------
# 2. 构建快照(fake 句柄)
# ---------------------------------------------------------------------------


def _rm(
    state="NORMAL", reason="", kill=False, kill_reason="", open_=False, open_reason="", silence=0.0
):
    return SimpleNamespace(
        state_machine=SimpleNamespace(current=state, reason=reason),
        kill_switch=SimpleNamespace(is_armed=kill, reason=kill_reason),
        breaker=SimpleNamespace(is_open=open_, reason=open_reason),
        ws_silence_seconds=silence,
    )


def _gate(fund_action="NONE", reconciled=True, exchange=True, connection=True, data=True):
    return SimpleNamespace(
        last_breaker_action=SimpleNamespace(value=fund_action),
        reconciled=reconciled,
        exchange_healthy=exchange,
        connection_ok=connection,
        market_data_healthy=data,
    )


def _sup(statuses, failure_count=0):
    return SimpleNamespace(status=lambda: statuses, failure_count=failure_count)


def _market(prices):
    return SimpleNamespace(state={s: SimpleNamespace(last_price=p) for s, p in prices.items()})


class TestBuildRuntimeHealth:
    def test_empty_state_is_safe_and_tolerates_missing(self):
        """未初始化 SystemState: 不抛异常, 返回「未运行」快照, 状态 SAFE。"""
        h = build_runtime_health(SystemState())
        assert h["status"] == STATUS_SAFE
        assert h["running"] is False
        assert h["uptime_seconds"] == 0.0
        assert h["lifecycle"]["state"] == "未初始化"
        assert h["risk"]["state"] == "未初始化"
        assert h["kill_switch"]["armed"] is False
        assert h["tasks"] == {
            "total": 0,
            "running": 0,
            "failed": 0,
            "failure_count": 0,
            "active": [],
        }
        assert h["trade"]["orders_total"] == 0
        assert h["last_event"] is None
        assert h["last_error"] is None

    def test_full_healthy_aggregation_is_trading(self):
        from at50_execution.observability import MetricsStore
        from at60_risk.system_lifecycle import SystemLifecycle

        lc = SystemLifecycle()
        lc.warm_up()
        lc.sync()
        lc.self_check()
        lc.ready()
        lc.start_trading()

        metrics = MetricsStore()
        metrics.incr("orders_total", 10)
        metrics.incr("orders_failed", 1)

        st = SystemState()
        st.running = True
        st.started_at = time.time() - 100.0
        st.risk_manager = _rm()
        st.lifecycle = lc
        st.trading_gate = _gate()
        st.metrics = metrics
        st.supervisor = _sup(
            [
                {"name": "risk-loop", "running": True, "exception": None},
                {"name": "reconcile-loop", "running": True, "exception": None},
                {"name": "web-server", "running": False, "exception": "boom"},
            ],
            failure_count=1,
        )
        st.market_engine = _market({"SOLUSDT": 123.45})
        st.last_reconcile_at = time.time() - 5.0
        st.last_error = {"ts": time.time(), "source": "对账", "message": "权益漂移"}

        h = build_runtime_health(st)

        assert h["status"] == STATUS_TRADING
        assert h["running"] is True
        assert 95.0 <= h["uptime_seconds"] <= 105.0
        assert h["lifecycle"]["state"] == "TRADING"
        assert h["risk"]["state"] == "NORMAL"
        assert h["kill_switch"]["armed"] is False
        assert h["breaker"]["open"] is False
        assert h["breaker"]["fund_action"] == "NONE"
        assert h["reconcile"]["reconciled"] is True
        assert h["reconcile"]["last_reconcile_at"] == st.last_reconcile_at
        assert h["exchange"]["healthy"] is True
        assert h["market"]["connection_ok"] is True
        assert h["market"]["data_healthy"] is True
        assert h["market"]["last_prices"] == {"SOLUSDT": 123.45}
        assert h["tasks"]["total"] == 3
        assert h["tasks"]["running"] == 2
        assert h["tasks"]["failed"] == 1
        assert h["tasks"]["failure_count"] == 1
        assert h["tasks"]["active"] == ["risk-loop", "reconcile-loop"]
        assert h["trade"]["orders_total"] == 10
        assert h["trade"]["orders_failed"] == 1
        assert h["trade"]["order_failure_rate"] == pytest.approx(0.1)
        assert h["last_event"]["to"] == "TRADING"
        assert h["last_error"]["source"] == "对账"

    def test_kill_switch_armed_classifies_killed(self):
        st = SystemState()
        st.running = True
        st.risk_manager = _rm(kill=True, kill_reason="人工急停")
        st.trading_gate = _gate()
        h = build_runtime_health(st)
        assert h["status"] == STATUS_KILLED
        assert h["kill_switch"]["armed"] is True
        assert h["kill_switch"]["reason"] == "人工急停"

    def test_task_snapshot_zero_running_when_supervisor_empty(self):
        st = SystemState()
        st.supervisor = _sup([])
        h = build_runtime_health(st)
        assert h["tasks"]["total"] == 0
        assert h["tasks"]["active"] == []
