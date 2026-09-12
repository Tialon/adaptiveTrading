"""状态人话化(V13 P0) —— 把内部状态机翻译成操作者能直接读懂的结论。

**这个模块解决什么**: 系统的真实状态由四套状态机 + 急停 + 熔断 + 对账 + 后台任务共同决定,
此前页面只能把它们各自的英文枚举并排铺开(SAFE_MODE / RECOVERY / DEGRADED / KILLED …),
于是**只有读过源码的人**才知道系统现在到底能不能交易、要不要自己动手。

本模块把 `at01_common/runtime_health.build_runtime_health()` 的快照翻译成**固定五段式**:

    结论 > 原因 > 影响 > 系统动作 > 用户动作

**边界(不可越界)**
- **只翻译, 不重判**: 交易许可(`can_buy`/`can_sell`)**直接取快照里的值**, 本模块绝不自己算一遍。
  快照本身又直接取自 `TradingGate`(`runtime_health.py:167-177`), 单一权威不在此处被绕开。
- **不救场**: 本模块**不改任何状态**、不做任何迁移、不解除任何冻结。它只回答「现在怎么样」。
  恢复动作在 `at50_risk/auto_recovery.py` 与 `/api/emergency/recover`, 各有各的门。
- **纯函数**: 无 I/O、无全局态、对缺失字段容错 —— 与 `runtime_health.py` 同风格, 便于独立审计。

**通知分级**(任务书 P0「用户通知模型」): 决定「这件事要不要打扰用户」, 五档语义严格区分:

    NORMAL          一切正常, 不打扰用户。
    NOTICE          值得记录, 但能力未降级(例: 今日 3 次 WS 重连, 均已自动恢复)。
    DEGRADED        能力下降, 系统继续运行并**自动恢复中**, 用户**无需操作**。
    ACTION_REQUIRED 真正需要人(API 权限变化 / 资金差异无法自动解释 / 策略版本待确认)。
    KILLED          交易已冻结, 需明确告知原因、系统已做了什么、当前资金状态、是否要人工处理。

**为什么 `requires_human` 默认偏保守**: 判定依据(急停来源)在 V13 之前并不存在,
拿不到来源时**一律按「需要人」处理** —— 宁可多喊一次人, 也不让系统在真钱模式下自行解冻。
`at50_risk/auto_recovery.py` 会带上真实来源, 那时才走自动化分支。
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# 通知分级(单一来源, 供页面/告警/测试引用)
# ---------------------------------------------------------------------------

LEVEL_NORMAL = "NORMAL"
LEVEL_NOTICE = "NOTICE"
LEVEL_DEGRADED = "DEGRADED"
LEVEL_ACTION_REQUIRED = "ACTION_REQUIRED"
LEVEL_KILLED = "KILLED"

# 分级 -> (标题词, 语气)。tone 供前端上色, 语义与 web_operator_status.risk_level 一致。
LEVEL_META: dict[str, dict[str, str]] = {
    LEVEL_NORMAL: {"label": "运行正常", "tone": "safe"},
    LEVEL_NOTICE: {"label": "有记录事项", "tone": "safe"},
    LEVEL_DEGRADED: {"label": "降级运行", "tone": "warning"},
    LEVEL_ACTION_REQUIRED: {"label": "需要你的确认", "tone": "danger"},
    LEVEL_KILLED: {"label": "交易已冻结", "tone": "blocked"},
}

# 严重度排序(值越大越严重), 供「取最严重项」用
LEVEL_SEVERITY: dict[str, int] = {
    LEVEL_NORMAL: 0,
    LEVEL_NOTICE: 1,
    LEVEL_DEGRADED: 2,
    LEVEL_ACTION_REQUIRED: 3,
    LEVEL_KILLED: 4,
}

# ---------------------------------------------------------------------------
# 急停来源(任务书: 「对人工主动 KILL 与重大资金异常 KILL 保留人工恢复确认」)
# ---------------------------------------------------------------------------

KILL_ORIGIN_MANUAL = "MANUAL"
KILL_ORIGIN_AUTO_RECONCILE = "AUTO_RECONCILE"    # 对账瞬态漂移
KILL_ORIGIN_AUTO_DATA = "AUTO_DATA"              # 行情数据失真
KILL_ORIGIN_AUTO_TASK = "AUTO_TASK"              # 关键后台任务崩溃
KILL_ORIGIN_AUTO_EQUITY = "AUTO_EQUITY"          # 权益异常 —— 重大资金
KILL_ORIGIN_AUTO_ACCOUNTING = "AUTO_ACCOUNTING"  # 账务事务失败 —— 重大资金

ALL_KILL_ORIGINS: tuple[str, ...] = (
    KILL_ORIGIN_MANUAL, KILL_ORIGIN_AUTO_RECONCILE, KILL_ORIGIN_AUTO_DATA,
    KILL_ORIGIN_AUTO_TASK, KILL_ORIGIN_AUTO_EQUITY, KILL_ORIGIN_AUTO_ACCOUNTING,
)

# 允许**自动恢复**的来源: 只限「系统自己造成的、条件恢复后能自证的」三类。
#
# 为什么 AUTO_EQUITY / AUTO_ACCOUNTING **不在**这里: 它们代表**账本与真实资产对不上**。
# 这种状态下「条件已恢复」本身就是无法自证的命题 —— 系统无法证明自己没算错,
# 所以必须由人核对交易所账户后才能解除(任务书: Unknown Financial State
# → stay frozen + human confirmation)。
SELF_HEALABLE_KILL_ORIGINS: frozenset[str] = frozenset(
    {KILL_ORIGIN_AUTO_RECONCILE, KILL_ORIGIN_AUTO_DATA, KILL_ORIGIN_AUTO_TASK}
)

# 重大资金异常来源(永远需要人)
MAJOR_FUND_KILL_ORIGINS: frozenset[str] = frozenset(
    {KILL_ORIGIN_AUTO_EQUITY, KILL_ORIGIN_AUTO_ACCOUNTING}
)


def _meta(level: str) -> dict[str, str]:
    return LEVEL_META.get(level, LEVEL_META[LEVEL_ACTION_REQUIRED])


# ---------------------------------------------------------------------------
# 阻断原因 -> 人话(关键词命中; 覆盖 TradingGate / 生命周期 / 风险态的全部拒绝串)
# ---------------------------------------------------------------------------

# 每条: (关键词, 原因, 影响, 用户动作)
# 关键词按**具体优先**排列 —— 先匹配到的赢, 所以「资金熔断」必须排在「熔断」之前。
_REASON_RULES: tuple[tuple[str, str, str, str], ...] = (
    (
        "急停",
        "系统处于急停冻结状态, 所有交易已被停止。",
        "无法买入, 也无法卖出减仓。",
        "@HUMAN_OR_AUTO_KILL",
    ),
    (
        "安全模式",
        "系统检测到数据或账户状态可信度不足, 已进入安全模式。",
        "无法开新仓; 数据可信时仍允许安全离场。",
        "确认数据来源与交易所账户状态恢复正常后, 系统会自动退出安全模式。",
    ),
    (
        "对账",
        "账户资产与系统账本尚未对齐, 系统拒绝在未对账的状态下交易。",
        "无法开新仓, 避免拿着错误的账本下单。",
        "无需操作, 系统正在自动重新对账; 若持续无法对齐会来通知你。",
    ),
    (
        "资金熔断",
        "账户资金与系统预期出现超出阈值的偏离, 已触发资金级熔断。",
        "本次熔断档位下禁止开新仓, 可能同时限制减仓。",
        "无需操作, 系统会持续复核; 若差异无法自动解释会要求你确认账户资产。",
    ),
    (
        "熔断",
        "触发了风险熔断保护。",
        "熔断档位内禁止开新仓。",
        "无需操作, 冷却结束后会自动复位; 持续不复位会来通知你。",
    ),
    (
        "关键后台任务",
        "系统的一个关键后台任务异常退出, 为保证安全已停止开仓。",
        "无法开新仓, 避免在监控缺失的情况下交易。",
        "无需操作, 系统正在自动重启该任务; 反复失败时会来通知你。",
    ),
    (
        "停机",
        "系统正在执行停机流程。",
        "所有交易动作已停止。",
        "无需操作; 若这是计划外停机, 重新启动服务即可。",
    ),
    (
        "系统已停止",
        "系统已停止运行。",
        "所有交易动作已停止。",
        "重新启动服务即可恢复。",
    ),
    (
        "恢复核验",
        "系统正在做恢复核验, 确认账户状态与对账结果一致后才允许交易。",
        "无法开新仓。",
        "无需操作, 核验通过后会自动恢复。",
    ),
    (
        "恢复中",
        "系统正在从异常中自动恢复。",
        "恢复期间无法开新仓, 必要时允许安全离场。",
        "无需操作, 系统会自己走完恢复流程。",
    ),
    (
        "启动中",
        "系统正在启动并完成自检(连接交易所 / 预热行情 / 对账)。",
        "启动完成前不会开新仓。",
        "无需操作, 等待启动完成即可。",
    ),
    (
        "降级",
        "系统处于降级运行状态。",
        "禁止开新仓, 允许安全离场。",
        "无需操作, 条件恢复后会自动回升到正常。",
    ),
    (
        "仅减仓",
        "风险档位已降到「仅减仓」, 只允许卖出不允许买入。",
        "无法买入; 仍可正常卖出减仓。",
        "无需操作, 等待回撤恢复或系统自动复核。",
    ),
    (
        "风险暂停",
        "风险保护触发, 交易已暂时暂停。",
        "暂停期间无法开新仓。",
        "无需操作, 暂停窗口到期后会自动恢复。",
    ),
    (
        "暂停",
        "风险保护触发, 交易已暂时暂停。",
        "暂停期间无法开新仓。",
        "无需操作, 暂停窗口到期后会自动恢复。",
    ),
    (
        "行情连接",
        "与交易所的行情连接尚未就绪。",
        "行情未就绪时不下单, 避免用陈旧价格成交。",
        "无需操作, 系统正在自动重连。",
    ),
    (
        "行情数据不健康",
        "行情数据未通过健康校验(可能静默或跳变)。",
        "数据不可信时不下单。",
        "无需操作, 系统正在自动恢复行情。",
    ),
    (
        "交易所不健康",
        "交易所接口或对账链路当前不可信。",
        "无法确认交易所状态时不下单。",
        "无需操作, 系统会持续探测并在恢复后自动放行。",
    ),
    (
        "风险禁止",
        "风险层拒绝了本次操作。",
        "本次操作不会执行。",
        "无需操作, 等待风险档位恢复。",
    ),
)

_GENERIC_REASON = (
    "系统当前不满足交易条件。",
    "在条件恢复前不会开新仓。",
    "无需操作, 系统会自动复核并在条件恢复后放行。",
)


def explain_block_reason(reason: str) -> dict[str, str]:
    """把闸门的中文阻断串翻译成「原因 / 影响 / 用户动作」。

    `@HUMAN_OR_AUTO_KILL` 是占位 —— 急停是否要人取决于**来源**, 由 `explain()` 决定后回填;
    单独调用本函数时按最保守处理(需要人)。
    """
    text = (reason or "").strip()
    for keyword, cause, impact, action in _REASON_RULES:
        if keyword in text:
            if action == "@HUMAN_OR_AUTO_KILL":
                action = (
                    "系统已停止交易并保留现场; 是否自动解除取决于冻结来源,"
                    "无法自动解释时会请你确认账户资产。"
                )
            return {"cause": cause, "impact": impact, "user_action": action}
    cause, impact, action = _GENERIC_REASON
    return {"cause": cause, "impact": impact, "user_action": action}


# ---------------------------------------------------------------------------
# 状态 -> 五段式模板
# ---------------------------------------------------------------------------

# 键为 runtime_health 的 7 个状态(runtime_health.STATUS_*)。
# (title, cause, impact, system_actions, user_action, next_step)
_STATUS_TEMPLATES: dict[str, tuple[str, str, str, list[str], str, str]] = {
    "TRADING": (
        "系统运行正常",
        "行情、交易所、对账、风控各维度均通过, 系统正在正常交易。",
        "买入与卖出均按交易闸门逐笔判定后执行。",
        ["持续监控行情与账户", "每笔订单在提交前重新过闸门", "每个对账周期自动核对账本"],
        "无需操作。",
        "系统将自动继续运行。",
    ),
    "SAFE": (
        "系统已就绪, 等待交易",
        "系统已完成启动自检, 当前没有正在进行的交易动作。",
        "具备交易条件时会在闸门放行后开仓。",
        ["保持行情与账户状态监控", "等待策略信号"],
        "无需操作。",
        "系统将自动继续运行。",
    ),
    "DEGRADED": (
        "系统正在降级运行",
        "部分能力受限, 系统主动收紧了交易动作以保证安全。",
        "禁止开新仓; 允许安全离场。",
        ["已收紧开仓许可", "持续复核受限维度", "条件恢复后自动回升"],
        "无需操作, 系统正在自动恢复。",
        "条件恢复后将自动回到正常交易。",
    ),
    "REDUCE_ONLY": (
        "系统处于仅减仓状态",
        "风险档位下降, 系统只允许卖出, 不允许买入。",
        "无法买入; 仍可卖出减仓。",
        ["已暂停买入", "保留卖出通道以便安全离场", "持续复核回撤与风险档位"],
        "无需操作, 系统会在风险恢复后自动放开买入。",
        "风险指标恢复后自动回到正常交易。",
    ),
    "PAUSED": (
        "系统已暂停交易",
        "触发了风险或资金熔断保护, 交易被暂时冻结。",
        "暂停期间无法开新仓。",
        ["已暂停交易动作", "持续复核触发条件", "冷却结束后自动复检"],
        "无需操作, 系统正在自动恢复。",
        "冷却结束后系统会自动复核并恢复。",
    ),
    "RECOVERY": (
        "系统正在恢复",
        "交易所账户数据暂时无法确认, 系统正在做恢复核验。",
        "恢复期间无法开新仓; 数据可信时允许安全离场。",
        ["已暂停开仓", "正在重新连接交易所", "正在重新同步账户并重新对账"],
        "无需操作, 系统正在自动恢复。",
        "系统将在下一个复核周期自动重试。",
    ),
    "KILLED": (
        "系统已停止交易",
        "触发了急停冻结, 交易已停止。",
        "无法买入, 也无法卖出减仓。",
        ["已冻结全部交易动作", "已尝试撤销未成交订单", "已保留现场证据"],
        "系统已停止交易; 是否自动解除取决于冻结来源, 无法自动解释时会请你确认账户资产。",
        "系统会持续复核冻结条件。",
    ),
    # 下列两项**不是** runtime 状态, 而是生命周期态 —— 因为 classify_runtime_status() 把
    # STOPPED 与 SAFE_MODE 都归并进状态 KILLED, 若只按状态取模板会把「进程已停」
    # 说成「急停冻结」, 语义完全不同。故按**更具体**的生命周期态优先取模板。
    "STOPPED": (
        "系统已停止运行",
        "服务已停止, 系统不再做任何交易动作。",
        "全部交易动作已停止, 且不会自行恢复。",
        ["已停止全部后台任务", "已按优雅停机流程落盘"],
        "需要你的操作: 重新启动服务后系统会自行完成启动自检。",
        "重启服务即可恢复。",
    ),
    "SAFE_MODE": (
        "系统处于安全模式",
        "系统检测到数据或账户状态可信度不足, 已进入安全模式。",
        "无法开新仓; 数据可信时仍允许安全离场。",
        ["已禁止开新仓", "已保留安全离场通道", "已记录进入安全模式的原因"],
        # ⚠️ 措辞与**当前实现**一致: `SystemLifecycle.exit_safe_mode()` 目前没有生产调用者
        # (对账 PASS 只回升 DEGRADED/RECOVERY, 不处理 SAFE_MODE), 所以现在确实需要人。
        # V13 W7 接入自动恢复后, 这里会改成「无需操作, 系统会自动复核并退出」。
        "需要你的操作: 确认行情数据与交易所账户恢复正常后, 重新启动服务或执行「恢复急停」。",
        "条件恢复后需要你确认一次, 系统随后回到正常交易。",
    ),
}

_FALLBACK_TEMPLATE: tuple[str, str, str, list[str], str, str] = (
    "系统状态暂不可读",
    "系统尚未完成初始化, 暂时读不到完整状态。",
    "初始化完成前不会开新仓。",
    ["等待启动完成", "启动完成后自动复核各维度"],
    "无需操作, 等待系统启动完成。",
    "系统完成启动后会自动刷新状态。",
)


# ---------------------------------------------------------------------------
# 分级判定
# ---------------------------------------------------------------------------


def classify_notice_level(health: dict[str, Any], *, requires_human: bool = True) -> str:
    """把健康快照归并到五档通知级别(纯函数)。

    `requires_human` 只对**冻结类**状态起作用 —— 决定冻结是「必须人工」还是「系统自愈中」。
    其余状态的级别由能力是否受损决定, 与来源无关。

    ⚠️ 判定顺序按**具体优先**: `classify_runtime_status()` 把 `STOPPED` 与 `SAFE_MODE`
    都归并为状态 `KILLED`, 所以这里必须先看生命周期, 否则「进程已停」会被当成
    「急停可自愈」—— 那是错的, 进程不在了就没有任何自动恢复可言。
    """
    status = str(health.get("status") or "")
    lifecycle = str((health.get("lifecycle") or {}).get("state") or "")

    # 1. 进程已停止: 没有自动恢复可言, 必须人重新拉起服务
    if lifecycle == "STOPPED":
        return LEVEL_ACTION_REQUIRED
    # 2. 安全模式: 对账 PASS 不会退出它(见 system_lifecycle.apply_reconcile_verdict),
    #    当前实现下确实需要人 —— 不谎称「会自动恢复」。
    if lifecycle == "SAFE_MODE":
        return LEVEL_ACTION_REQUIRED
    # 3. 冻结(急停 / 风险 KILLED)
    if status == "KILLED":
        return LEVEL_KILLED if requires_human else LEVEL_DEGRADED
    # 4. 资金熔断已判 KILL 但状态机尚未反映(两个动作之间有窗口):
    #    金融状态不明 → 保持冻结 + 要人(任务书: Unknown Financial State)。
    breaker = health.get("breaker") or {}
    if str(breaker.get("fund_action") or "") == "KILL":
        return LEVEL_ACTION_REQUIRED
    # 5. 自动恢复中的降级
    if status in ("RECOVERY", "DEGRADED", "REDUCE_ONLY", "PAUSED"):
        return LEVEL_DEGRADED
    # 6. 正常运行 / 就绪
    if status in ("TRADING", "SAFE"):
        return LEVEL_NORMAL
    # 7. 状态缺失 —— 未初始化, 不值得打扰用户
    return LEVEL_NOTICE


def kill_requires_human(origin: str) -> bool:
    """急停是否**必须**人工解除。

    fail-closed: 来源为空、无法识别、或属于重大资金异常 → 需要人。
    只有 `SELF_HEALABLE_KILL_ORIGINS` 三类才允许走自动恢复。
    """
    return (origin or "").strip() not in SELF_HEALABLE_KILL_ORIGINS


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def resolve_situation(health: dict[str, Any]) -> str:
    """决定用哪套模板 —— 返回 `_STATUS_TEMPLATES` 的键。

    **为什么不直接用 `health["status"]`**: `classify_runtime_status()` 把
    `STOPPED` / `SAFE_MODE` / 风险 `KILLED` / 急停 四种**语义完全不同**的情形
    都归并成了状态 `KILLED`。对分类器而言那是正确的(它们都「不可交易」),
    但对**解释**而言必须区分 —— 用户需要知道的是「进程停了要重启」还是
    「急停了等系统自愈」, 这两件事的操作完全不同。
    """
    lifecycle = str((health.get("lifecycle") or {}).get("state") or "")
    if lifecycle == "STOPPED":
        return "STOPPED"
    if lifecycle == "SAFE_MODE":
        return "SAFE_MODE"
    # 状态 KILLED = 急停 armed 或风险态 KILLED —— 两者对用户都是「交易已冻结」,
    # 不必再分开讲(风险态已由 classify_runtime_status 反映进 status)。
    return str(health.get("status") or "")


def explain(
    health: dict[str, Any],
    *,
    mode_label: str = "",
    kill_origin: str = "",
    extra_actions: list[str] | None = None,
) -> dict[str, Any]:
    """把健康快照翻译成操作者结论(五段式)。

    `health` 必须是 `build_runtime_health()` 的输出 —— 本函数不重判交易许可,
    只读取其中的 `can_buy`/`can_sell`/`status` 及各维信号。

    `kill_origin` 为急停来源(见 `AUTO_KILL_ORIGINS`)。V13 之前没有这个信息,
    缺省空串 → 按「需要人」处理(fail-closed)。
    """
    health = health or {}
    status = str(health.get("status") or "")
    situation = resolve_situation(health)
    title, cause, impact, actions, user_action, next_step = _STATUS_TEMPLATES.get(
        situation, _FALLBACK_TEMPLATE
    )
    actions = list(actions)

    lifecycle = health.get("lifecycle") or {}
    risk = health.get("risk") or {}
    kill = health.get("kill_switch") or {}
    breaker = health.get("breaker") or {}
    tasks = health.get("tasks") or {}

    # 只有「真的被急停冻结」才谈来源; SAFE_MODE/STOPPED 不是急停, 不存在来源问题。
    # 资金熔断 KILL 属于**重大资金异常**, 无论来源一律要人(fail-closed, 不交给自动恢复)。
    is_kill = situation == "KILLED"
    fund_kill = str(breaker.get("fund_action") or "NONE") == "KILL"
    requires_human = (kill_requires_human(kill_origin) or fund_kill) if is_kill else False
    level = classify_notice_level(health, requires_human=requires_human)
    if situation == "STOPPED":
        level = LEVEL_ACTION_REQUIRED

    # ---- 原因: 真实字段优先。模板保证非空, 这些是「为什么」的具体化
    sentences: list[str] = [cause]
    detail_bits: list[str] = []
    for source in (lifecycle.get("reason"), risk.get("reason"), kill.get("reason"),
                   breaker.get("reason")):
        text = str(source or "").strip()
        if text and text not in detail_bits:
            detail_bits.append(text)
    if detail_bits:
        sentences.append(f"具体原因: {'; '.join(detail_bits)}。")

    # ---- 阻断说明: 把闸门的真实拒绝串翻译进来(买入侧优先, 与页面口径一致)
    buy_reason = str(health.get("buy_block_reason") or "").strip()
    sell_reason = str(health.get("sell_block_reason") or "").strip()
    primary = buy_reason or sell_reason
    if primary:
        block = explain_block_reason(primary)
        if block["cause"] not in sentences:
            sentences.append(block["cause"])
        impact = block["impact"]
        # 系统自己在恢复的场景, 用阻断项给出的动作更贴合(「等待重连」而不是笼统的「无需操作」)
        if level == LEVEL_DEGRADED:
            user_action = block["user_action"]

    # ---- 能力受限时如实列出「暂停了什么」
    if not bool(health.get("can_buy", False)):
        actions.append("已暂停买入(BUY)")
    if not bool(health.get("can_sell", False)):
        actions.append("已暂停卖出(SELL)")

    # ---- 后台任务异常: 属「系统自动处理」类, 但值得如实列出
    failed = int(tasks.get("failed") or 0)
    if failed > 0:
        actions.append(f"检测到 {failed} 个后台任务异常, 正在自动重启")
        if level == LEVEL_NORMAL:
            level = LEVEL_NOTICE

    fund_action = str(breaker.get("fund_action") or "NONE")
    if fund_action != "NONE":
        actions.append(f"资金熔断档位: {fund_action}")

    if extra_actions:
        actions.extend(a for a in extra_actions if a)

    # 去重保持顺序 —— 重复行会让「系统已经做了什么」看起来像凑数
    deduped: list[str] = []
    for a in actions:
        if a not in deduped:
            deduped.append(a)

    # ---- 冻结类: 按来源改写用户动作与分级(本模块唯一「有条件」的分支)
    if situation == "KILLED":
        if requires_human:
            level = LEVEL_KILLED
            user_action = (
                "需要你的确认: 急停由人工或资金异常触发, 系统不会自行解除。"
                "请核对 Binance 账户资产与系统账本一致后, 再执行「恢复急停」。"
            )
            next_step = "等待你确认账户状态后恢复。"
        else:
            level = LEVEL_DEGRADED
            user_action = "无需操作, 系统正在自动复核冻结条件并尝试自愈。"
            next_step = "条件全部复核通过后系统会自动恢复。"

    # ---- 需要人 ≠ 冻结: 统一确保「需要人」的状态一定说清要人做什么
    if level == LEVEL_ACTION_REQUIRED and not requires_human:
        requires_human = True
        if situation in ("KILLED", "STOPPED", "SAFE_MODE"):
            pass  # 各自的模板/分支已给出更具体的文案, 不覆盖
        else:
            user_action = (
                "需要你的确认: 账户资金状态无法自动解释, 系统保持冻结。"
                "请核对 Binance 当前账户资产后确认。"
            )

    meta = _meta(level)
    return {
        "level": level,
        "level_label": meta["label"],
        "tone": meta["tone"],
        "title": title,
        "conclusion": _conclusion(title, level, mode_label),
        "cause": " ".join(sentences),
        "impact": impact,
        "system_actions": deduped,
        "user_action": user_action,
        "requires_human": requires_human,
        "next_step": next_step,
        "state": status or "UNKNOWN",
        "situation": situation,
    }


def _conclusion(title: str, level: str, mode_label: str) -> str:
    """一句话结论 —— 页面第一行会直接读它, 所以要能独立成立(不依赖上下文)。"""
    prefix = f"{mode_label}运行中。" if mode_label else ""
    if level == LEVEL_NORMAL:
        return f"{prefix}系统运行正常, 无需操作。"
    if level == LEVEL_NOTICE:
        return f"{prefix}{title}, 暂无需操作。"
    if level == LEVEL_DEGRADED:
        return f"{prefix}{title} —— 系统正在自动恢复, 无需操作。"
    if level == LEVEL_ACTION_REQUIRED:
        return f"{prefix}{title} —— 需要你确认后才能继续。"
    return f"{prefix}{title} —— 系统不会自行解除, 请查看下方说明。"


# ---------------------------------------------------------------------------
# 交易结论人话化(任务书 P0「交易结果必须可解释」)
# ---------------------------------------------------------------------------


def _fmt_pct(value: Any) -> str:
    try:
        return f"{float(value) * 100:.2f}%"
    except (TypeError, ValueError):
        return "--"


def explain_trade(
    *,
    side: str,
    symbol: str,
    status: str = "",
    price: float | None = None,
    quantity: float | None = None,
    quote: float | None = None,
    slippage: float | None = None,
    latency_ms: float | None = None,
    score: float | None = None,
    regime: str = "",
    trend: str = "",
    money_flow: str = "",
    risk_notes: list[str] | None = None,
) -> dict[str, Any]:
    """生成一条「人话交易结论」。

    设计给两类读者: 人(页面直接展示) 与 AI(结构化字段可直接进 review package)。
    因此返回**结构化字段 + 预渲染标题**, 而不是一段纯文本 —— 纯文本没法被程序再消费。
    """
    side_upper = (side or "").upper()
    verb = "买入" if side_upper == "BUY" else "卖出" if side_upper == "SELL" else (side or "交易")
    filled = (status or "").upper() == "FILLED"

    reasons: list[str] = []
    if regime:
        reasons.append(f"市场状态: {regime}")
    if trend:
        reasons.append(f"趋势: {trend}")
    if money_flow:
        reasons.append(f"资金流: {money_flow}")
    if score is not None:
        reasons.append(f"策略评分: {score}")

    execution: list[str] = []
    if status:
        execution.append(f"订单: {status}")
    if slippage is not None:
        execution.append(f"滑点: {_fmt_pct(slippage)}")
    if latency_ms is not None:
        execution.append(f"耗时: {int(latency_ms)}ms")

    return {
        "title": f"{verb} {symbol}",
        "result": "成功" if filled else (status or "进行中"),
        "side": side_upper,
        "symbol": symbol,
        "price": price,
        "quantity": quantity,
        "quote": quote,
        "why": reasons,
        "risk": list(risk_notes or []),
        "execution": execution,
    }


def summarize_trade(trade: dict[str, Any]) -> str:
    """把 `explain_trade()` 的产物压成一行(供事件流/日报用)。"""
    parts = [str(trade.get("title") or "交易")]
    if trade.get("result"):
        parts.append(str(trade["result"]))
    if trade.get("price") is not None:
        parts.append(f"@ {trade['price']}")
    if trade.get("quantity") is not None:
        parts.append(f"× {trade['quantity']}")
    return " ".join(parts)
