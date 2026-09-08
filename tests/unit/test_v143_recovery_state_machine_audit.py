"""V11.4 P0-4 恢复状态机穷举审计(RiskStateMachine 全迁移矩阵 + 方向闸门 + 恢复不变量)。

审计目标: 把「恢复状态机」的所有状态迁移、方向闸门、恢复两步语义一次性钉死,
防止未来改动引入「某异常态可逃逸到可交易」的回归。

覆盖:
1. RiskStateMachine 5 态 × 6 操作 = 30 格全迁移矩阵(数据驱动穷举);
2. RiskStateMachine 5 态方向闸门矩阵(can_buy / can_sell / can_trade);
3. SystemLifecycle 10 态迁移合法性(合法迁移到位 / 非法迁移拒绝不漂移);
4. 恢复不变量: 两步恢复(禁止裸 reset)、RECOVERY_CHECK 冻结(pause/reduce_only 不降级)、
   kill_switch 兜底(状态机即使漂移, 急停开关仍禁开)。

审计发现(已修复): 此前 `pause()` / `reduce_only()` 从 RECOVERY_CHECK 会降级到
PAUSED / REDUCE_ONLY, 与文档「RECOVERY_CHECK 优先于 PAUSED / REDUCE_ONLY」矛盾,
且 PAUSED 时间窗自动恢复会绕过 confirm_recovered 两步确认。已改为与 KILLED 同冻结。
"""

import pytest

from at60_risk.risk_state import RiskState, RiskStateMachine
from at60_risk.system_lifecycle import LifecycleState, SystemLifecycle

# ---------------------------------------------------------------------------
# 辅助: 驱动状态机到指定态 / 应用操作
# ---------------------------------------------------------------------------

_STATES = [
    RiskState.NORMAL,
    RiskState.PAUSED,
    RiskState.REDUCE_ONLY,
    RiskState.KILLED,
    RiskState.RECOVERY_CHECK,
]


def _in_state(state: RiskState) -> RiskStateMachine:
    """从 NORMAL 经真实迁移驱动到指定态。"""
    sm = RiskStateMachine(pause_seconds=60.0)
    if state is RiskState.PAUSED:
        sm.pause("audit")
    elif state is RiskState.REDUCE_ONLY:
        sm.reduce_only("audit")
    elif state is RiskState.KILLED:
        sm.kill("audit")
    elif state is RiskState.RECOVERY_CHECK:
        sm.kill("audit")
        sm.reset()
    return sm


def _apply(sm: RiskStateMachine, op: str) -> None:
    if op == "pause":
        sm.pause("audit-op")
    elif op == "reduce_only":
        sm.reduce_only("audit-op")
    elif op == "recover":
        sm.recover()
    elif op == "kill":
        sm.kill("audit-op")
    elif op == "reset":
        sm.reset()
    elif op == "confirm_recovered":
        sm.confirm_recovered()
    else:  # pragma: no cover - 防御
        raise AssertionError(f"未知操作 {op}")


# 全迁移矩阵: (source, op) -> expected state
_TRANSITIONS = {
    (RiskState.NORMAL, "pause"): RiskState.PAUSED,
    (RiskState.NORMAL, "reduce_only"): RiskState.REDUCE_ONLY,
    (RiskState.NORMAL, "recover"): RiskState.NORMAL,
    (RiskState.NORMAL, "kill"): RiskState.KILLED,
    (RiskState.NORMAL, "reset"): RiskState.NORMAL,
    (RiskState.NORMAL, "confirm_recovered"): RiskState.NORMAL,

    (RiskState.PAUSED, "pause"): RiskState.PAUSED,
    (RiskState.PAUSED, "reduce_only"): RiskState.REDUCE_ONLY,
    (RiskState.PAUSED, "recover"): RiskState.NORMAL,
    (RiskState.PAUSED, "kill"): RiskState.KILLED,
    (RiskState.PAUSED, "reset"): RiskState.PAUSED,
    (RiskState.PAUSED, "confirm_recovered"): RiskState.PAUSED,

    (RiskState.REDUCE_ONLY, "pause"): RiskState.PAUSED,
    (RiskState.REDUCE_ONLY, "reduce_only"): RiskState.REDUCE_ONLY,
    (RiskState.REDUCE_ONLY, "recover"): RiskState.NORMAL,
    (RiskState.REDUCE_ONLY, "kill"): RiskState.KILLED,
    (RiskState.REDUCE_ONLY, "reset"): RiskState.REDUCE_ONLY,
    (RiskState.REDUCE_ONLY, "confirm_recovered"): RiskState.REDUCE_ONLY,

    (RiskState.KILLED, "pause"): RiskState.KILLED,
    (RiskState.KILLED, "reduce_only"): RiskState.KILLED,
    (RiskState.KILLED, "recover"): RiskState.KILLED,
    (RiskState.KILLED, "kill"): RiskState.KILLED,
    (RiskState.KILLED, "reset"): RiskState.RECOVERY_CHECK,
    (RiskState.KILLED, "confirm_recovered"): RiskState.KILLED,

    (RiskState.RECOVERY_CHECK, "pause"): RiskState.RECOVERY_CHECK,
    (RiskState.RECOVERY_CHECK, "reduce_only"): RiskState.RECOVERY_CHECK,
    (RiskState.RECOVERY_CHECK, "recover"): RiskState.RECOVERY_CHECK,
    (RiskState.RECOVERY_CHECK, "kill"): RiskState.KILLED,
    (RiskState.RECOVERY_CHECK, "reset"): RiskState.RECOVERY_CHECK,
    (RiskState.RECOVERY_CHECK, "confirm_recovered"): RiskState.NORMAL,
}


def _tid(source: RiskState, op: str) -> str:
    return f"{source.value}.{op}"


@pytest.mark.parametrize(
    "source,op,expected",
    [(*k, v) for k, v in _TRANSITIONS.items()],
    ids=[_tid(s, o) for (s, o) in _TRANSITIONS],
)
def test_risk_transition_matrix(source, op, expected):
    """5 态 × 6 操作全矩阵: 每格迁移后的终态与规格一致。"""
    sm = _in_state(source)
    _apply(sm, op)
    assert sm.state is expected


# ---------------------------------------------------------------------------
# 方向闸门矩阵
# ---------------------------------------------------------------------------

# (source, can_buy, can_sell, can_trade)
_GATE_MATRIX = {
    RiskState.NORMAL: (True, True, True),
    RiskState.PAUSED: (False, False, False),
    RiskState.REDUCE_ONLY: (False, True, False),
    RiskState.KILLED: (False, False, False),
    RiskState.RECOVERY_CHECK: (False, False, False),
}


@pytest.mark.parametrize("state", _STATES, ids=[s.value for s in _STATES])
def test_risk_gate_matrix(state):
    """方向闸门: 仅 NORMAL 可买; 仅 NORMAL/REDUCE_ONLY 可卖; 仅 NORMAL 可完全交易。"""
    sm = _in_state(state)
    can_buy, can_sell, can_trade = _GATE_MATRIX[state]
    assert sm.can_buy() is can_buy, f"{state.value}.can_buy"
    assert sm.can_sell() is can_sell, f"{state.value}.can_sell"
    assert sm.can_trade() is can_trade, f"{state.value}.can_trade"


# ---------------------------------------------------------------------------
# SystemLifecycle 迁移合法性(合法迁移到位 / 非法拒绝不漂移)
# ---------------------------------------------------------------------------

_LC_LEGAL = [
    ("warm_up", LifecycleState.INIT, LifecycleState.WARMING_UP),
    ("sync", LifecycleState.WARMING_UP, LifecycleState.SYNCING),
    ("self_check", LifecycleState.SYNCING, LifecycleState.SELF_CHECK),
    ("ready", LifecycleState.SELF_CHECK, LifecycleState.READY),
    ("start_trading", LifecycleState.READY, LifecycleState.TRADING),
]


def _lc_in(state: LifecycleState) -> SystemLifecycle:
    """构造处于指定生命周期态的实例(真实迁移序列)。"""
    lc = SystemLifecycle()
    for m, tgt in (
        ("warm_up", LifecycleState.WARMING_UP),
        ("sync", LifecycleState.SYNCING),
        ("self_check", LifecycleState.SELF_CHECK),
        ("ready", LifecycleState.READY),
        ("start_trading", LifecycleState.TRADING),
    ):
        if lc.state is state:
            return lc
        getattr(lc, m)()
    if lc.state is LifecycleState.TRADING and state is LifecycleState.DEGRADED:
        lc.degrade("audit")
    elif lc.state is LifecycleState.TRADING and state is LifecycleState.RECOVERY:
        lc.degrade("audit")
        lc.recover()
    elif lc.state is LifecycleState.TRADING and state is LifecycleState.SAFE_MODE:
        lc.enter_safe_mode("audit")
    elif lc.state is LifecycleState.TRADING and state is LifecycleState.STOPPED:
        lc.stop()
    return lc


@pytest.mark.parametrize(
    "op,src,tgt",
    _LC_LEGAL,
    ids=[f"{s.value}->{t.value}" for _, s, t in _LC_LEGAL],
)
def test_lifecycle_legal_transitions(op, src, tgt):
    """启动链 5 步线性迁移到位。"""
    lc = _lc_in(src)
    assert getattr(lc, op)() is True
    assert lc.state is tgt


@pytest.mark.parametrize(
    "op,src",
    [
        ("sync", LifecycleState.INIT),
        ("self_check", LifecycleState.INIT),
        ("start_trading", LifecycleState.INIT),
        ("start_trading", LifecycleState.WARMING_UP),
        ("warm_up", LifecycleState.TRADING),
        ("degrade", LifecycleState.INIT),
    ],
)
def test_lifecycle_illegal_transitions_rejected(op, src):
    """非法迁移被拒绝且不漂移(状态保持)。"""
    lc = _lc_in(src)
    before = lc.state
    if op in ("degrade", "enter_safe_mode"):
        result = getattr(lc, op)("audit")
    else:
        result = getattr(lc, op)()
    assert result is False
    assert lc.state is before


def test_lifecycle_stop_terminal_and_safe_mode_exit():
    """STOPPED 终态; SAFE_MODE 仅 exit -> READY。"""
    lc = _lc_in(LifecycleState.TRADING)
    assert lc.stop() is True
    assert lc.state is LifecycleState.STOPPED
    assert lc.degrade("x") is False  # 停止后不再迁移

    lc2 = _lc_in(LifecycleState.TRADING)
    assert lc2.enter_safe_mode("audit") is True
    assert lc2.state is LifecycleState.SAFE_MODE
    assert lc2.exit_safe_mode() is True
    assert lc2.state is LifecycleState.READY


# ---------------------------------------------------------------------------
# 恢复不变量(两步恢复 + RECOVERY_CHECK 冻结 + kill_switch 兜底)
# ---------------------------------------------------------------------------

def test_two_step_recovery_no_naked_reset():
    """禁止裸 reset: KILLED 只能经 RECOVERY_CHECK 再 confirm 回 NORMAL。"""
    sm = _in_state(RiskState.KILLED)
    sm.reset()
    assert sm.state is RiskState.RECOVERY_CHECK
    assert not sm.can_buy()
    # 直接 recover 不能跳过 confirm
    sm.recover()
    assert sm.state is RiskState.RECOVERY_CHECK
    sm.confirm_recovered()
    assert sm.state is RiskState.NORMAL


def test_recovery_check_frozen_against_pause_and_reduce_only():
    """审计修复: RECOVERY_CHECK 冻结, pause/reduce_only 不降级(与 KILLED 同)。"""
    sm = _in_state(RiskState.RECOVERY_CHECK)
    assert sm.pause("行情静默") is False
    assert sm.state is RiskState.RECOVERY_CHECK
    assert sm.reduce_only("权益漂移") is False
    assert sm.state is RiskState.RECOVERY_CHECK


def test_kill_switch_is_defense_in_depth_during_recovery():
    """兜底: 恢复核验期间即使状态机漂移, 急停开关仍禁开(人工 disarm 才解除)。"""
    from at60_risk.risk_manager import RiskManager

    rm = RiskManager()
    rm.kill_switch.arm("权益漂移")
    rm.state_machine.kill("权益漂移")
    rm.state_machine.reset()
    # 即使状态机被异常降级为 PAUSED 并时间窗自动恢复 NORMAL, kill_switch 仍兜底禁买
    rm.state_machine.pause("行情静默")
    assert not rm.can_buy()  # kill_switch armed
    rm.state_machine.confirm_recovered()
    assert not rm.can_buy()  # 仍未 disarm
    rm.kill_switch.disarm()
    assert rm.can_buy()
