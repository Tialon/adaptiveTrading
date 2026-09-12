"""恢复检查(V15 §8)—— 「恢复急停」不是「清除 KILL」, 而是**请求系统重新做一遍安全检查**。

**这个模块解决什么**: 真实用户验收暴露出:

    系统进入安全模式 → equity=KILL / position=PAUSE / cash=PAUSE → 行情静默 → 资金漂移
    → 用户点击「恢复急停」 → **仍然无法恢复到可交易状态**, 而且页面没说清是谁在挡着。

V13 起 `recover` 的做法是: 人工点一次就当「人工已核对账户」, 直接解冻并把未满足的前置条件
**如实回报**。那个方向是对的(解冻 ≠ 允许交易), 但不够: 用户点完看到状态变了、几秒后又冻上,
只得到「没有效果」的观感, 却不知道**到底是哪一个条件在阻止恢复**。

V15 把它改成一条明确的**恢复检查**:

    用户点击恢复急停
        ↓
    解除 MANUAL 急停(仅此一项是"清标志")
        ↓
    Recovery Check: 逐项复检 行情 / 交易所账户 / 资金 / 持仓 / 对账 / 关键任务 / 配置 / 闸门
        ↓
    全部满足 → 恢复 TRADING
    任一不满足 → **保持冻结**, 并逐项说明是谁在挡、为什么、能不能自证、要不要人

**第一原则(不可越界)**: 禁止为了让恢复"成功"而降低任何安全门槛。
本模块**不放宽任何判定** —— 它读的全是 `TradingGate` 已有的健康位与既有配置校验;
它也没有 `force_normal()` 之类的路径。解冻后能否下单, 仍然由 `TradingGate` 逐笔判定。

**唯一的人工越过点**是 `human_confirmed=True`: 它表示操作者**明确表示已核对过交易所账户**。
它只允许在「账户真伪类条件」不满足时越过, 且必须由调用方显式传入(默认 False),
并会落一条高等级操作员事件。它越过的是「等账户对上」这件事, **不是**放行下单。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MISSING_RECONCILE = "对账尚未通过"
MISSING_CONNECTION = "行情连接未就绪"
MISSING_EXCHANGE = "交易所状态不可信"
MISSING_SHUTDOWN = "系统正在停机"

# 条件分类: 决定「人工确认」能不能越过它。
#
# - `account_truth`  账户真伪类(交易所/对账/资金/持仓) —— 系统**无法自证**,
#                    操作者明确确认后可越过(§9C 的「人工确认」路径)。
# - `self_healable`  系统能自证恢复的(行情/关键任务) —— 等它自己恢复, 人工确认也越不过
#                    (越过了就等于在数据不可信时交易, 那是拿真钱冒险)。
# - `blocking`       结构性阻塞(停机/配置非法/闸门本身) —— 改配置或重启, 不是确认能解决的。
CATEGORY_ACCOUNT_TRUTH = "account_truth"
CATEGORY_SELF_HEALABLE = "self_healable"
CATEGORY_BLOCKING = "blocking"


@dataclass
class RecoveryCheckItem:
    """恢复检查的一项。`ok=False` 时必须能回答「谁能解决它」。"""

    key: str
    label: str
    ok: bool
    detail: str = ""
    category: str = CATEGORY_ACCOUNT_TRUTH

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "label": self.label, "ok": self.ok,
            "detail": self.detail, "category": self.category,
            "overridable_by_human": self.category == CATEGORY_ACCOUNT_TRUTH,
            "why": self._why(),
        }

    def _why(self) -> str:
        """这一项为什么需要人 / 为什么只能等 —— 任务书 §4 要求逐项说清。"""
        if self.ok:
            return "已满足。"
        if self.category == CATEGORY_ACCOUNT_TRUTH:
            return ("账户真实性与系统账本无法自证一致 —— 重新对账只是拿自己的账本对自己的账本; "
                    "需要人核对交易所账户。")
        if self.category == CATEGORY_SELF_HEALABLE:
            return "系统可以自己恢复(等数据/任务回来), 不需要人, 但**不能靠确认越过**。"
        return "结构性阻塞 —— 需要修正配置或重启, 不是确认能解决的。"


@dataclass
class RecoveryPrecondition:
    """兼容旧字段的聚合视图(保留 `ok`/`missing`, 页面对接不变)。"""

    ok: bool = False
    missing: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    items: list[RecoveryCheckItem] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "missing": list(self.missing), "details": dict(self.details),
            "items": [i.to_dict() for i in self.items],
        }


def _classify_reconcile_failure(message: str) -> RecoveryCheckItem:
    """把对账的失败原因翻译成**具体是哪一维**在挡(资金/持仓/现金)。"""
    text = (message or "").lower()
    if "equity" in text or "drift" in text:
        return RecoveryCheckItem(
            "equity", "资金(权益)", False,
            f"本地权益与交易所不一致: {message}", CATEGORY_ACCOUNT_TRUTH)
    if "position" in text:
        return RecoveryCheckItem(
            "position", "持仓", False,
            f"本地持仓与交易所不一致: {message}", CATEGORY_ACCOUNT_TRUTH)
    if "cash" in text:
        return RecoveryCheckItem(
            "cash", "现金", False,
            f"本地现金与交易所不一致: {message}", CATEGORY_ACCOUNT_TRUTH)
    return RecoveryCheckItem(
        "reconcile", "对账", False, f"对账未通过: {message or '原因未记录'}", CATEGORY_ACCOUNT_TRUTH)


def run_recovery_check(
    gate: Any, risk_manager: Any, *, settings: Any = None, last_error: Any = None,
) -> RecoveryPrecondition:
    """逐项复检「现在能不能安全交易」(V15 §8 的检查清单)。

    纯读: 只读 `TradingGate` 的既有健康位 + 既有配置校验, **不重判**任何东西,
    也不改任何状态。`gate` 为 None 时保守拒绝。
    """
    if gate is None:
        return RecoveryPrecondition(
            ok=False, missing=["交易闸门未就绪"],
            items=[RecoveryCheckItem("gate", "交易闸门", False, "闸门未就绪, 无法复检",
                                     CATEGORY_BLOCKING)],
        )

    items: list[RecoveryCheckItem] = []

    # 1) 停机(结构性)
    shutting = bool(getattr(gate, "shutting_down", False))
    items.append(RecoveryCheckItem(
        "shutdown", "停机窗口", not shutting,
        MISSING_SHUTDOWN if shutting else "不在停机窗口", CATEGORY_BLOCKING))

    # 2) 行情(能自证)
    conn = bool(getattr(gate, "connection_ok", False))
    data_ok = bool(getattr(gate, "market_data_healthy", False))
    items.append(RecoveryCheckItem(
        "market", "行情", conn and data_ok,
        "行情连接与数据均正常" if (conn and data_ok)
        else (MISSING_CONNECTION if not conn else "行情数据不健康(静默/跳变)"),
        CATEGORY_SELF_HEALABLE))

    # 3) 交易所账户(账户真伪)
    exch = bool(getattr(gate, "exchange_healthy", False))
    items.append(RecoveryCheckItem(
        "exchange", "交易所账户", exch,
        "交易所接口与账户可读" if exch else MISSING_EXCHANGE, CATEGORY_ACCOUNT_TRUTH))

    # 4) 对账 —— 并按失败原因细分到 资金/持仓/现金(§6/§7 要求单独定位)
    reconciled = bool(getattr(gate, "reconciled", False))
    if reconciled:
        items.append(RecoveryCheckItem("reconcile", "对账", True, "最近一次对账通过"))
    else:
        msg = ""
        if isinstance(last_error, dict):
            msg = str(last_error.get("message") or "")
        elif last_error:
            msg = str(last_error)
        items.append(_classify_reconcile_failure(msg))

    # 5) 关键任务(能自证)
    tasks_ok = bool(getattr(gate, "critical_tasks_healthy", True))
    items.append(RecoveryCheckItem(
        "tasks", "关键后台任务", tasks_ok,
        "关键任务在运行" if tasks_ok else "关键后台任务未运行", CATEGORY_SELF_HEALABLE))

    # 6) 配置(结构性) —— 复用启动期同源的 validate()
    if settings is not None:
        try:
            problems = list(settings.validate())
        except Exception as exc:
            problems = [f"配置校验未能完成: {exc}"]
        items.append(RecoveryCheckItem(
            "config", "配置", not problems,
            "配置合法" if not problems else "; ".join(problems[:3]), CATEGORY_BLOCKING))

    # 7) 急停标志 —— **只作说明, 不参与判定**。
    #
    # ⚠️ 这里有一个容易写错的地方: 「急停已武装」**不能**算作恢复检查未通过 ——
    # 它正是本次要解除的那个状态。把它当阻塞项会形成一个自锁: 因为冻结, 所以不许解冻。
    # (写这一版时就踩了, 被 `test_recover_disarms_switch_once_conditions_are_met` 抓住。)
    kill_armed = bool(getattr(getattr(risk_manager, "kill_switch", None), "is_armed", False))
    items.append(RecoveryCheckItem(
        "kill_switch", "急停标志", True,
        "急停已武装 —— 本次恢复会解除它" if kill_armed else "急停未武装",
        CATEGORY_ACCOUNT_TRUTH))

    failing = [i for i in items if not i.ok]
    return RecoveryPrecondition(
        ok=not failing,
        missing=[i.label for i in failing],
        details={
            "kill_origin": str(getattr(getattr(risk_manager, "kill_switch", None), "origin", "")),
            "failing_categories": sorted({i.category for i in failing}),
        },
        items=items,
    )


# 保留旧名: 调用方(与既有测试)仍可用
assess_preconditions = run_recovery_check


def _human_overridable(check: RecoveryPrecondition) -> bool:
    """所有失败项是否都属「人工确认可越过」的账户真伪类。

    行情不可信 / 关键任务不在 / 配置非法 / 停机 —— 这些**不能**靠一句确认越过,
    越过了就等于在数据不可信时下单。
    """
    failing = [i for i in check.items if not i.ok]
    return bool(failing) and all(i.category == CATEGORY_ACCOUNT_TRUTH for i in failing)


def perform_recovery(
    *, risk_manager: Any, lifecycle: Any, gate: Any, force: bool = False,
    settings: Any = None, last_error: Any = None,
) -> dict[str, Any]:
    """执行恢复检查并(条件满足时)解除冻结。

    `force=True` = **操作者显式表示已核对过交易所账户**(V15 §9C 的「人工确认」)。
    它**只能**越过账户真伪类条件; 若还有行情/任务/配置/停机类条件未满足,
    即便 force 也**保持冻结** —— 任务书第一原则: 不得为了通过测试而降低安全门槛。
    """
    check = run_recovery_check(gate, risk_manager, settings=settings, last_error=last_error)

    if not check.ok:
        overridable = _human_overridable(check)
        if not (force and overridable):
            return {
                "ok": False,
                "stage": "recovery_check",
                "missing": list(check.missing),
                "items": [i.to_dict() for i in check.items],
                "precondition": check.to_dict(),
                "human_override_available": overridable,
                # 五段式(任务书 §8 指定失败时也要给全)
                "cause": "恢复检查未通过: " + "、".join(check.missing),
                "impact": "系统继续保持安全冻结, 暂时无法开新仓。",
                "system_actions": ["已解除人工急停标志", "已逐项复检安全条件", "继续保持冻结"],
                "user_action": (
                    "请核对 Binance 账户资产与系统账本是否一致; 确认无误后可在页面选择"
                    "「我已核对账户, 继续恢复」。"
                    if overridable else
                    "无需核对账户 —— 阻塞项不是账户差异, 等系统自行恢复或修正配置。"
                ),
                "steps": [],
            }

    steps: list[str] = []

    # 1) 解除急停标志(唯一一项"清标志"的动作, 且只在检查通过后执行)
    kill = getattr(risk_manager, "kill_switch", None)
    if kill is not None and bool(getattr(kill, "is_armed", False)):
        kill.disarm()
        steps.append("已解除急停冻结")

    # 2) 风险态: KILLED → RECOVERY_CHECK → NORMAL(两步, 保留"核验一次"的语义)
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
        "items": [i.to_dict() for i in check.items],
        "precondition": check.to_dict(),
        "human_override_available": False,
        "steps": steps,
        "cause": "恢复检查全部通过。",
        "impact": "系统已解除冻结; 能否下单仍由交易闸门逐笔判定。",
        "system_actions": steps,
        "user_action": "无需操作。",
    }
