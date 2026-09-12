"""V11.1 P1-3 System Lifecycle 顶层状态机单元测试。

覆盖: 状态迁移(合法/非法)/ CanTrade 四维闸门 / CanReduce 减仓闸门 / 纯函数 trading_gate·reduce_gate。
"""

import pytest

from at50_risk.risk_state import RiskState
from at50_risk.system_lifecycle import (
    LifecycleState,
    SystemLifecycle,
    reduce_gate,
    trading_gate,
)


def _go_trading() -> SystemLifecycle:
    """走完启动链到 TRADING。"""
    lc = SystemLifecycle()
    assert lc.warm_up()
    assert lc.sync()
    assert lc.self_check()
    assert lc.ready()
    assert lc.start_trading()
    return lc


# ---------------------------------------------------------------------------
# 迁移
# ---------------------------------------------------------------------------

def test_initial_state():
    assert SystemLifecycle().state is LifecycleState.INIT


def test_linear_startup_chain():
    lc = SystemLifecycle()
    assert lc.warm_up()
    assert lc.sync()
    assert lc.self_check()
    assert lc.ready()
    assert lc.start_trading()
    assert lc.state is LifecycleState.TRADING


def test_invalid_transition_rejected():
    lc = SystemLifecycle()
    assert lc.start_trading() is False  # INIT 不能直接 TRADING
    assert lc.state is LifecycleState.INIT
    assert lc.sync() is False  # INIT 不能直接 SYNCING
    assert lc.state is LifecycleState.INIT


def test_degrade_recover_cycle():
    lc = _go_trading()
    assert lc.degrade("权益漂移")
    assert lc.state is LifecycleState.DEGRADED
    assert lc.recover()
    assert lc.state is LifecycleState.RECOVERY
    assert lc.ready()
    assert lc.state is LifecycleState.READY


def test_safe_mode_from_any_and_exit():
    lc = _go_trading()
    assert lc.enter_safe_mode("账本重建歧义")
    assert lc.state is LifecycleState.SAFE_MODE
    assert lc.exit_safe_mode()
    assert lc.state is LifecycleState.READY


def test_safe_mode_not_from_stopped():
    lc = _go_trading()
    lc.stop()
    assert lc.enter_safe_mode("x") is False
    assert lc.state is LifecycleState.STOPPED


def test_stop_is_terminal():
    lc = _go_trading()
    assert lc.stop()
    assert lc.is_stopped
    # 停止后不再迁移
    assert lc.degrade("x") is False
    assert lc.enter_safe_mode("x") is False
    assert lc.state is LifecycleState.STOPPED


# ---------------------------------------------------------------------------
# trading_gate(纯函数)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "state,reason_part",
    [
        (LifecycleState.INIT, "启动中"),
        (LifecycleState.WARMING_UP, "启动中"),
        (LifecycleState.SYNCING, "启动中"),
        (LifecycleState.SELF_CHECK, "启动中"),
        (LifecycleState.DEGRADED, "降级中"),
        (LifecycleState.RECOVERY, "恢复中"),
        (LifecycleState.SAFE_MODE, "安全模式"),
        (LifecycleState.STOPPED, "停止"),
    ],
)
def test_trading_gate_blocks_non_trading_lifecycle(state, reason_part):
    ok, reason = trading_gate(state, RiskState.NORMAL, True, True)
    assert not ok
    assert reason_part in reason


def test_trading_gate_allows_ready_and_trading():
    for state in (LifecycleState.READY, LifecycleState.TRADING):
        ok, reason = trading_gate(state, RiskState.NORMAL, True, True)
        assert ok, reason


def test_trading_gate_connection_and_reconcile():
    # 缺连接或对账 → 拒绝
    assert not trading_gate(LifecycleState.TRADING, RiskState.NORMAL, False, True)[0]
    assert not trading_gate(LifecycleState.TRADING, RiskState.NORMAL, True, False)[0]


def test_trading_gate_risk_states():
    for rs in (RiskState.KILLED, RiskState.RECOVERY_CHECK, RiskState.PAUSED, RiskState.REDUCE_ONLY):
        ok, reason = trading_gate(LifecycleState.TRADING, rs, True, True)
        assert not ok, rs
    assert trading_gate(LifecycleState.TRADING, RiskState.NORMAL, True, True)[0]


# ---------------------------------------------------------------------------
# reduce_gate(纯函数)
# ---------------------------------------------------------------------------

def test_reduce_gate_allows_during_degraded():
    # 降级/恢复期仍可安全离场
    assert reduce_gate(LifecycleState.DEGRADED, RiskState.NORMAL, True)[0]
    assert reduce_gate(LifecycleState.RECOVERY, RiskState.REDUCE_ONLY, True)[0]


def test_reduce_gate_allows_reduce_only_risk():
    assert reduce_gate(LifecycleState.TRADING, RiskState.REDUCE_ONLY, True)[0]


def test_reduce_gate_blocks_killed_and_safe_mode():
    assert not reduce_gate(LifecycleState.TRADING, RiskState.KILLED, True)[0]
    assert not reduce_gate(LifecycleState.SAFE_MODE, RiskState.NORMAL, True)[0]
    assert not reduce_gate(LifecycleState.STOPPED, RiskState.NORMAL, True)[0]


def test_reduce_gate_blocks_startup_and_no_connection():
    assert not reduce_gate(LifecycleState.INIT, RiskState.NORMAL, True)[0]
    assert not reduce_gate(LifecycleState.TRADING, RiskState.NORMAL, False)[0]


# ---------------------------------------------------------------------------
# SystemLifecycle 闸门方法
# ---------------------------------------------------------------------------

def test_lifecycle_can_trade_method():
    lc = _go_trading()
    assert lc.can_trade(RiskState.NORMAL, True, True)[0]
    lc.degrade("x")
    ok, reason = lc.can_trade(RiskState.NORMAL, True, True)
    assert not ok
    assert "降级" in reason
