"""恢复流程(V13 P0)—— 解除冻结的**唯一实现**。

**这个模块解决什么(一个真实的 bug)**: `POST /api/emergency/recover` 此前只调
`kill_switch.disarm()`, 而**没有**碰 `RiskStateMachine` 与 `SystemLifecycle`:

- `RiskStateMachine.reset()` / `confirm_recovered()`(KILLED → RECOVERY_CHECK → NORMAL)
- `SystemLifecycle.exit_safe_mode()`

这两组方法在全代码库里**没有任何生产调用者**(只有测试)。后果是: 回撤 15% 触发
`state_machine.kill()` 或进入 SAFE_MODE 之后, **只能靠重启进程脱身** ——
两个状态机都是内存态, 重启即回到初值。这不是设计上的保守, 是恢复链路根本没接完。

Pi 上此刻卡在 SAFE_MODE + KILLED 就是这一处的现场证据。

**为什么把流程抽成一个模块**: 解除冻结有两个入口 —— 人工(`/api/emergency/recover`)
与自动(`at50_risk/auto_recovery.py`)。两者**必须走同一段代码**, 否则迟早出现
「人工能恢复的状态自动恢复不了」或反之, 而那是排查起来最费劲的一类不一致。

**边界(不可越界)**
- 本模块**不放宽任何判定**: 前置条件全部读 `TradingGate` 的既有健康位, 前置不满足就
  原地不动并如实说明还差什么。
- 解除冻结**不等于允许下单**: 解冻后能否成交仍由 `TradingGate` 逐笔判定。
- **不碰资金**: 全程没有任何下单/撤单/转账动作。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MISSING_RECONCILE = "对账尚未通过"
MISSING_CONNECTION = "行情连接未就绪"
MISSING_EXCHANGE = "交易所状态不可信"
MISSING_SHUTDOWN = "系统正在停机"


@dataclass
class RecoveryPrecondition:
    """恢复前置条件核对结果。`ok=False` 时**不得**执行任何解冻动作。"""

    ok: bool = False
    missing: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "missing": list(self.missing), "details": dict(self.details)}


def assess_preconditions(gate: Any, risk_manager: Any) -> RecoveryPrecondition:
    """核对「账户状态是否已可信」—— 对应任务书的恢复链:

        重新连接 → 重新获取账户 → 重新对账 → 重检行情 → 重检风控 → 全部正常

    逐条映射到闸门的既有健康位(闸门本身就是这些维度的单一权威, 不另起一套判定)。
    `gate` 为 None 时**保守拒绝** —— 读不到健康位时不动任何状态。
    """
    if gate is None:
        return RecoveryPrecondition(ok=False, missing=["交易闸门未就绪"])

    missing: list[str] = []
    if bool(getattr(gate, "shutting_down", False)):
        missing.append(MISSING_SHUTDOWN)
    if not bool(getattr(gate, "connection_ok", False)):
        missing.append(MISSING_CONNECTION)
    if not bool(getattr(gate, "market_data_healthy", False)):
        missing.append("行情数据不健康")
    if not bool(getattr(gate, "exchange_healthy", False)):
        missing.append(MISSING_EXCHANGE)
    if not bool(getattr(gate, "reconciled", False)):
        missing.append(MISSING_RECONCILE)
    if not bool(getattr(gate, "critical_tasks_healthy", True)):
        missing.append("关键后台任务未运行")

    return RecoveryPrecondition(
        ok=not missing,
        missing=missing,
        details={
            "connection_ok": bool(getattr(gate, "connection_ok", False)),
            "market_data_healthy": bool(getattr(gate, "market_data_healthy", False)),
            "exchange_healthy": bool(getattr(gate, "exchange_healthy", False)),
            "reconciled": bool(getattr(gate, "reconciled", False)),
            "critical_tasks_healthy": bool(getattr(gate, "critical_tasks_healthy", True)),
            "kill_origin": str(getattr(getattr(risk_manager, "kill_switch", None), "origin", "")),
        },
    )


def perform_recovery(
    *, risk_manager: Any, lifecycle: Any, gate: Any, force: bool = False
) -> dict[str, Any]:
    """执行解除冻结。返回逐步结果(供页面/事件流展示)。

    `force=True` 跳过前置核对 —— **只应由人工入口使用**: 人有能力核对交易所账户,
    系统没有。「人工点恢复」本身就是一次人工确认。

    `force=False`(自动恢复用)时前置不满足 → 原地不动。
    """
    check = assess_preconditions(gate, risk_manager)
    if not check.ok and not force:
        return {
            "ok": False, "stage": "precondition", "missing": check.missing,
            "steps": [], "precondition": check.to_dict(),
        }

    steps: list[str] = []

    # 1) 解除急停标志
    kill = getattr(risk_manager, "kill_switch", None)
    if kill is not None and bool(getattr(kill, "is_armed", False)):
        kill.disarm()
        steps.append("已解除急停冻结")

    # 2) 风险态: KILLED → RECOVERY_CHECK → NORMAL
    #    必须走两步 —— `reset()` 刻意禁止裸跳到 NORMAL(见 risk_state.py 的迁移表),
    #    保留「核验一次」的语义。
    sm = getattr(risk_manager, "state_machine", None)
    if sm is not None:
        state = str(getattr(getattr(sm, "state", None), "value", "") or "")
        if state == "KILLED":
            sm.reset()
            state = "RECOVERY_CHECK"
            steps.append("风险态: 急停 → 恢复核验")
        if state == "RECOVERY_CHECK":
            sm.confirm_recovered()
            steps.append("风险态: 恢复核验 → 正常")
        elif state in ("PAUSED", "REDUCE_ONLY"):
            sm.recover()
            steps.append(f"风险态: {state} → 正常")

    # 3) 生命周期: SAFE_MODE → READY → TRADING
    if lifecycle is not None:
        current = str(getattr(lifecycle, "current", "") or "")
        if current == "SAFE_MODE":
            lifecycle.exit_safe_mode()
            current = str(getattr(lifecycle, "current", "") or "")
            steps.append("生命周期: 安全模式 → 就绪")
        if current in ("DEGRADED", "RECOVERY"):
            lifecycle.ready()
            current = str(getattr(lifecycle, "current", "") or "")
            steps.append("生命周期: 恢复 → 就绪")
        if current == "READY":
            lifecycle.start_trading()
            steps.append("生命周期: 就绪 → 交易中")

    if not steps:
        steps.append("系统本就未处于冻结状态")

    return {
        "ok": True,
        "stage": "recovered",
        "missing": [],
        "steps": steps,
        "precondition": check.to_dict(),
    }
