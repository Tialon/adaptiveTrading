"""系统生命周期状态机(V11.1 P1-3)

顶层状态机, 描述「系统从启动到交易到停止」的完整生命周期, 与底层的 `RiskStateMachine`
(风险五态 NORMAL/REDUCE_ONLY/PAUSED/KILLED/RECOVERY_CHECK)解耦、互补:
- RiskState 回答「风险上能不能交易」;
- Lifecycle 回答「系统整体就绪到哪一步了」。

CanTrade 由「生命周期态 + 风险态 + 连接状态 + 对账状态」四者共同决定(见 `trading_gate` /
`reduce_gate` 纯函数), 避免任何单一维度误放行。

状态:
    INIT → WARMING_UP → SYNCING → SELF_CHECK → READY ⇄ TRADING
                                     ↑              ↓
                                     └── DEGRADED ←─┘(degrade/recover)
    SAFE_MODE(任意可入, 仅 exit → READY)
    STOPPED(终态, 任意可入)
"""

from enum import Enum

from at01_common.logger import LoggerMixin
from at60_risk.risk_state import RiskState


class LifecycleState(str, Enum):
    INIT = "INIT"
    WARMING_UP = "WARMING_UP"
    SYNCING = "SYNCING"
    SELF_CHECK = "SELF_CHECK"
    READY = "READY"
    TRADING = "TRADING"
    DEGRADED = "DEGRADED"
    RECOVERY = "RECOVERY"
    SAFE_MODE = "SAFE_MODE"
    STOPPED = "STOPPED"


# 启动早期(不可交易)态集合
_STARTUP = frozenset({LifecycleState.INIT, LifecycleState.WARMING_UP, LifecycleState.SYNCING, LifecycleState.SELF_CHECK})


def trading_gate(
    lifecycle_state: LifecycleState,
    risk_state: RiskState,
    connection_ok: bool,
    reconciled: bool,
) -> tuple[bool, str]:
    """顶层 CanTrade(可开新仓 + 可减仓): 四者共同决定, 任一未就绪即拒绝并给原因。

    纯函数, 无副作用, 便于独立测试。
    """
    if lifecycle_state is LifecycleState.STOPPED:
        return False, "系统已停止"
    if lifecycle_state is LifecycleState.SAFE_MODE:
        return False, "安全模式"
    if lifecycle_state in _STARTUP:
        return False, f"启动中({lifecycle_state.value})"
    if lifecycle_state is LifecycleState.RECOVERY:
        return False, "恢复中"
    if lifecycle_state is LifecycleState.DEGRADED:
        return False, "降级中"
    # READY / TRADING
    if not connection_ok:
        return False, "行情连接未就绪"
    if not reconciled:
        return False, "对账未通过"
    if risk_state is RiskState.KILLED:
        return False, "急停中"
    if risk_state is RiskState.RECOVERY_CHECK:
        return False, "恢复核验中"
    if risk_state is RiskState.PAUSED:
        return False, "风险暂停"
    if risk_state is RiskState.REDUCE_ONLY:
        return False, "仅减仓"
    return True, ""


def reduce_gate(
    lifecycle_state: LifecycleState,
    risk_state: RiskState,
    connection_ok: bool,
) -> tuple[bool, str]:
    """减仓/离场闸门(CanReduce): 降级/恢复期仍允许安全离场, 但 SAFE_MODE/启动早期/停止不允许。

    - 风险态允许 can_sell(NORMAL 或 REDUCE_ONLY)。
    - 生命周期态允许 READY / TRADING / DEGRADED / RECOVERY。
    """
    if lifecycle_state is LifecycleState.STOPPED:
        return False, "系统已停止"
    if lifecycle_state is LifecycleState.SAFE_MODE:
        return False, "安全模式"
    if lifecycle_state in _STARTUP:
        return False, f"启动中({lifecycle_state.value})"
    if not connection_ok:
        return False, "行情连接未就绪"
    if not risk_state in (RiskState.NORMAL, RiskState.REDUCE_ONLY):
        return False, "风险禁止减仓"
    return True, ""


class SystemLifecycle(LoggerMixin):
    """顶层生命周期状态机(纯内存, 迁移集中校验)。"""

    def __init__(self):
        self._state = LifecycleState.INIT
        self._reason = ""

    # ---------- 读取 ----------

    @property
    def state(self) -> LifecycleState:
        return self._state

    @property
    def current(self) -> str:
        return self._state.value

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def is_stopped(self) -> bool:
        return self._state is LifecycleState.STOPPED

    @property
    def is_safe_mode(self) -> bool:
        return self._state is LifecycleState.SAFE_MODE

    # ---------- 迁移 ----------

    def warm_up(self) -> bool:
        return self._move(LifecycleState.WARMING_UP, {LifecycleState.INIT})

    def sync(self) -> bool:
        return self._move(LifecycleState.SYNCING, {LifecycleState.WARMING_UP})

    def self_check(self) -> bool:
        return self._move(LifecycleState.SELF_CHECK, {LifecycleState.SYNCING})

    def ready(self) -> bool:
        return self._move(LifecycleState.READY, {LifecycleState.SELF_CHECK, LifecycleState.DEGRADED, LifecycleState.RECOVERY})

    def start_trading(self) -> bool:
        return self._move(LifecycleState.TRADING, {LifecycleState.READY})

    def degrade(self, reason: str) -> bool:
        return self._move(LifecycleState.DEGRADED, {LifecycleState.READY, LifecycleState.TRADING}, reason)

    def recover(self) -> bool:
        return self._move(LifecycleState.RECOVERY, {LifecycleState.DEGRADED})

    def enter_safe_mode(self, reason: str) -> bool:
        # 除 STOPPED 外任意可入 SAFE_MODE
        return self._move(LifecycleState.SAFE_MODE, set(LifecycleState) - {LifecycleState.STOPPED}, reason)

    def exit_safe_mode(self) -> bool:
        return self._move(LifecycleState.READY, {LifecycleState.SAFE_MODE})

    def stop(self) -> bool:
        return self._move(LifecycleState.STOPPED, set(LifecycleState) - {LifecycleState.STOPPED})

    # ---------- 闸门 ----------

    def can_trade(self, risk_state: RiskState, connection_ok: bool, reconciled: bool) -> tuple[bool, str]:
        return trading_gate(self._state, risk_state, connection_ok, reconciled)

    def can_reduce(self, risk_state: RiskState, connection_ok: bool) -> tuple[bool, str]:
        return reduce_gate(self._state, risk_state, connection_ok)

    # ---------- 内部 ----------

    def _move(self, target: LifecycleState, allowed_from: set[LifecycleState], reason: str = "") -> bool:
        """校验迁移合法性, 非法/同态返回 False(记 warning), 合法则迁移并返回 True。"""
        if self._state is target:
            return False
        if self._state not in allowed_from:
            self.logger.warning("非法生命周期迁移", from_=self._state.value, to=target.value)
            return False
        self._state = target
        self._reason = reason
        self.logger.info("生命周期迁移", to=target.value, reason=reason)
        return True
