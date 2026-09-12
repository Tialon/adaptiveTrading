"""操作者状态聚合(只读展示层) —— Dashboard 首屏结论卡 / `/ops` 部署检查页的数据源。

**边界(不可越界)**
- 交易许可**单一权威**: `can_buy` / `can_sell` / `status` 全部取自
  `at01_common.runtime_health`(其本身又直接取自 `TradingGate`)。本模块**不实现**任何交易判定,
  也不放宽 `TradingGate` / `mainnet_readiness` / `testnet_gate` / `settings.validate()`。
- 只**读** settings 的开关做「模式判定」与「人话解释」, 不修改任何配置。
- **不返回密钥**: `WEB_ADMIN_TOKEN` 只暴露 `configured: true/false`, 绝不回显其值。

设计为纯函数(无 I/O、无全局状态), 便于独立测试与审计。
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# 运行模式(四种组合, 由 settings 的两个开关决定)
# ---------------------------------------------------------------------------

MODE_META: dict[str, dict[str, str]] = {
    "paper_testnet": {
        "label": "纸面 + 测试网",
        "detail": "不会使用真实资金, 也不连主网。适合 Pi 首次部署验证与策略迭代。",
        "tone": "safe",
    },
    "live_testnet": {
        "label": "测试网真实下单",
        "detail": "会向币安测试网发出真实订单(测试币), 不涉及真实资金。",
        "tone": "caution",
    },
    "paper_mainnet": {
        "label": "纸面 + 主网行情",
        "detail": "用主网真实行情做纸面模拟, 不发出真实订单。",
        "tone": "caution",
    },
    "live_mainnet": {
        "label": "主网真实资金",
        "detail": "会使用真实资金在币安主网下单。任何写操作都直接影响真实资产。",
        "tone": "danger",
    },
}

# 需要二次确认的危险写操作(急停不在此列 —— 冻结是安全方向, 应即时可用)
DANGEROUS_ACTIONS: tuple[str, ...] = (
    "emergency_recover",
    "breaker_reset",
    "shutdown",
)


def resolve_mode(*, paper_trading: bool, binance_testnet: bool) -> str:
    """由两个开关解析运行模式标识(四选一)。"""
    if paper_trading:
        return "paper_testnet" if binance_testnet else "paper_mainnet"
    return "live_testnet" if binance_testnet else "live_mainnet"


# ---------------------------------------------------------------------------
# 状态字典(与 docs/operating-modes-manual.md §5 保持一致)
# ---------------------------------------------------------------------------

STATUS_META: dict[str, dict[str, str]] = {
    "SAFE": {
        "label": "就绪 / 启动中",
        "detail": "尚未进入交易, 或交易闸门未完全就绪。",
        "risk_level": "warning",
    },
    "TRADING": {
        "label": "运行中",
        "detail": "可按交易闸门执行买卖。",
        "risk_level": "safe",
    },
    "DEGRADED": {
        "label": "降级",
        "detail": "禁止开新仓, 允许安全离场。",
        "risk_level": "warning",
    },
    "REDUCE_ONLY": {
        "label": "只减仓",
        "detail": "禁止买入, 只允许卖出减仓。",
        "risk_level": "warning",
    },
    "PAUSED": {
        "label": "暂停",
        "detail": "熔断或风控暂停, 等待冷却或人工检查。",
        "risk_level": "danger",
    },
    "RECOVERY": {
        "label": "恢复核验",
        "detail": "等待对账确认, 禁止开仓。",
        "risk_level": "danger",
    },
    "KILLED": {
        "label": "急停冻结",
        "detail": "重启不会自动恢复, 需要人工恢复。",
        "risk_level": "blocked",
    },
}

_UNKNOWN_STATUS = {
    "label": "未知状态",
    "detail": "状态快照暂不可读, 请查看容器日志。",
    "risk_level": "warning",
}

# ---------------------------------------------------------------------------
# 阻断原因 -> 下一步建议(按关键词命中, 兜底给通用建议)
# ---------------------------------------------------------------------------

_ACTION_HINTS: tuple[tuple[str, str], ...] = (
    ("急停", "确认本地与交易所差异已收敛后, 再执行「恢复急停」; 重启不会自动解除冻结。"),
    ("对账", "核对本地持仓/权益与交易所实际值, 差异收敛后再恢复交易。"),
    ("行情", "检查行情连接与数据静默时长, 等待行情恢复。"),
    ("交易所", "检查交易所连通性与接口健康, 等待对账恢复正常。"),
    ("关键后台任务", "检查后台任务是否有崩溃, 查看容器日志确认。"),
    ("停机", "系统正在停机, 等待停止完成或重新启动。"),
    ("熔断", "查看熔断原因; 冷却后会自动复位, 必要时人工解除。"),
    ("仅减仓", "处于只减仓档位, 等待回撤恢复或人工检查。"),
    ("暂停", "风险暂停中, 等待冷却结束或人工检查。"),
    ("恢复核验", "等待对账确认完成, 期间禁止开仓。"),
    ("降级", "检查降级原因, 条件恢复后会自动回升。"),
    ("启动", "等待系统完成启动与自检。"),
    ("安全模式", "系统处于安全模式, 确认数据可信后恢复。"),
)

_GENERIC_ACTION = "查看容器日志与 /api/metrics 的 health 快照确认原因。"


def suggest_next_action(status: str, block_reason: str) -> str:
    """由状态与阻断原因给出「下一步该做什么」(中文人话)。"""
    reason = (block_reason or "").strip()
    if reason:
        for keyword, hint in _ACTION_HINTS:
            if keyword in reason:
                return hint
    if status == "TRADING":
        return "无需操作, 系统运行正常。"
    return _GENERIC_ACTION


# ---------------------------------------------------------------------------
# 开关人话解释(P3: 让操作者不必读 .env)
# ---------------------------------------------------------------------------


def explain_switches(
    *,
    paper_trading: bool,
    binance_testnet: bool,
    live_trading_confirm: str,
    mainnet_api_scope_confirmed: bool,
    web_admin_token_configured: bool,
    auth_disabled: bool = False,
) -> list[dict[str, Any]]:
    """把关键开关翻译成人话。`WEB_ADMIN_TOKEN` 只报「已配置/未配置」, 不回显值。"""
    live_confirmed = (live_trading_confirm or "").strip().lower() == "true"
    return [
        {
            "key": "PAPER_TRADING",
            "value": "true" if paper_trading else "false",
            "text": (
                "纸面模式: 订单只做模拟, 不使用真实资金。"
                if paper_trading
                else "真实执行: 订单会真实发往交易所。"
            ),
            "tone": "safe" if paper_trading else "danger",
        },
        {
            "key": "BINANCE_TESTNET",
            "value": "true" if binance_testnet else "false",
            "text": (
                "连接币安测试网, 与真实资金隔离。"
                if binance_testnet
                else "连接币安主网, 面对真实市场与真实资金。"
            ),
            "tone": "safe" if binance_testnet else "danger",
        },
        {
            "key": "LIVE_TRADING_CONFIRM",
            "value": "true" if live_confirmed else "(空)",
            "text": (
                "主网守卫已解除。"
                if live_confirmed
                else "主网守卫生效: 未显式确认, 连主网会被拒绝启动。"
            ),
            "tone": "danger" if live_confirmed else "safe",
        },
        {
            "key": "MAINNET_API_SCOPE_CONFIRM",
            "value": "true" if mainnet_api_scope_confirmed else "false",
            "text": (
                "已人工确认主网 key 权限(仅 Spot、关提现/资金转移)。"
                if mainnet_api_scope_confirmed
                else "未确认主网 key 权限, 主网就绪自检会拦截。"
            ),
            "tone": "danger" if mainnet_api_scope_confirmed else "safe",
        },
        {
            "key": "WEB_ADMIN_TOKEN",
            "value": "已配置" if web_admin_token_configured else "未配置",
            "text": (
                "写操作已启用(急停/恢复/解除熔断/停机需令牌)。"
                if web_admin_token_configured
                else "写操作未启用: 所有写接口返回 503, 面板按钮不可用。"
            ),
            "tone": "safe" if web_admin_token_configured else "caution",
        },
        {
            "key": "WEB_ADMIN_AUTH",
            "value": "已关闭" if auth_disabled else "on",
            "text": (
                "写接口**不再校验令牌**: 局域网内任何设备都能改配置、恢复急停、停机。"
                if auth_disabled
                else "写接口需要令牌(X-Admin-Token)。"
            ),
            "tone": "danger" if auth_disabled else "safe",
        },
    ]


# ---------------------------------------------------------------------------
# 主聚合
# ---------------------------------------------------------------------------


_MODE3_LABELS: dict[str, str] = {"paper": "模拟", "testnet": "测试网", "live": "实盘"}


def _mode3(settings: Any) -> str:
    """三模式视图: 模拟 / 测试网 / 实盘。

    V12.7 起这是**操作者视角**的模式名。原来的 `mode`(paper_testnet/live_testnet/
    paper_mainnet/live_mainnet)是「纸面 × 交易所」的底层组合, 保留不动(向后兼容),
    但界面上以三模式为主。
    """
    if bool(getattr(settings, "paper_trading", True)):
        return "paper"
    return "testnet" if bool(getattr(settings, "binance_testnet", True)) else "live"


def _guard_override_state(settings: Any) -> dict[str, Any]:
    """V12.6 P2: 启动守卫解锁状态(实时判定, 过期即 false)。

    未配置时返回 `{"active": False, "configured": False}`, 页面据此不显示任何横幅 ——
    **只有真的解锁了才提示**, 否则天天挂个「未解锁」的红条反而稀释了告警的意义。
    """
    raw = str(getattr(settings, "guard_override", "") or "")
    if not raw.strip():
        return {"active": False, "configured": False}
    from at01_common.guard_override import parse_guard_override

    state = parse_guard_override(raw).to_dict()
    state["configured"] = True
    return state


def build_operator_status(settings: Any, health: dict[str, Any]) -> dict[str, Any]:
    """把 settings + runtime health 聚合成操作者结论(只读、可读、不抛异常)。

    `health` 必须是 `at01_common.runtime_health.build_runtime_health()` 的输出;
    本函数**不重新判定**交易许可, 只做翻译与组织。
    """
    health = health or {}
    can_buy = bool(health.get("can_buy"))
    can_sell = bool(health.get("can_sell"))
    buy_reason = str(health.get("buy_block_reason") or "")
    sell_reason = str(health.get("sell_block_reason") or "")

    status = str(health.get("status") or "")
    meta = STATUS_META.get(status, _UNKNOWN_STATUS)
    if not status:
        status = "SAFE"
        meta = _UNKNOWN_STATUS

    mode = resolve_mode(
        paper_trading=bool(settings.paper_trading),
        binance_testnet=bool(settings.binance_testnet),
    )
    mode_meta = MODE_META[mode]

    # 主要阻断原因: 优先买入侧, 其次卖出侧
    primary_reason = buy_reason or sell_reason

    # 买入与卖出两侧都要说清楚 —— 尤其 REDUCE_ONLY 的关键信息是「还能卖」。
    parts: list[str] = []
    if can_buy:
        parts.append("允许买入")
    else:
        parts.append(f"禁止买入({buy_reason or meta['detail']})")
    if can_sell:
        parts.append("允许卖出" + ("减仓" if not can_buy else ""))
    else:
        parts.append(f"禁止卖出({sell_reason or '原因未知'})")
    summary = f"{meta['label']}: " + "; ".join(parts)

    reconcile = health.get("reconcile") or {}
    tasks = health.get("tasks") or {}

    auth_disabled = bool(getattr(settings, "admin_auth_disabled", False))
    write_enabled = auth_disabled or bool(settings.web_admin_token)

    return {
        "auth_disabled": auth_disabled,
        "auth_notice": (
            "⚠️ 写接口鉴权已关闭(WEB_ADMIN_AUTH=off): 局域网内任何设备无需令牌即可"
            "改配置、恢复急停、停机。"
            if auth_disabled else ""
        ),
        "deploy": {
            "app": str(getattr(settings, "app_name", "")),
            "version": str(getattr(settings, "app_version", "")),
            "git_sha": str(getattr(settings, "git_sha", "") or ""),
            "image_tag": str(getattr(settings, "image_tag", "") or ""),
        },
        "runtime": {
            "running": bool(health.get("running")),
            "uptime_seconds": health.get("uptime_seconds"),
            "last_error": health.get("last_error"),
            "last_reconcile_at": reconcile.get("last_reconcile_at"),
            "reconciled": bool(reconcile.get("reconciled")),
            "tasks_failed": int(tasks.get("failed") or 0),
        },
        "mode": mode,
        "mode_label": mode_meta["label"],
        "mode_detail": mode_meta["detail"],
        "mode_tone": mode_meta["tone"],
        "risk_level": meta["risk_level"],
        "status": status,
        "status_label": meta["label"],
        "status_detail": meta["detail"],
        "can_buy": can_buy,
        "can_sell": can_sell,
        "buy_block_reason": buy_reason,
        "sell_block_reason": sell_reason,
        "block_reason": primary_reason,
        "summary": summary,
        "next_action": suggest_next_action(status, primary_reason),
        # V12.7: 三模式视图(任务单 §15)。**新增字段, 不改既有字段** —— 向后兼容:
        # 原来的 `mode`(paper_testnet/live_testnet/...) 保持原样, 前端可继续用。
        "trading_mode": _mode3(settings),
        "trading_mode_label": _MODE3_LABELS.get(_mode3(settings), "未知"),
        "market_data_source": "testnet" if bool(settings.binance_testnet) else "mainnet",
        "write_actions_enabled": write_enabled,
        "dangerous_actions": list(DANGEROUS_ACTIONS),
        # V12.6 P2: 启动守卫解锁状态 —— **必须暴露**, 否则「解锁了」这件事会变成隐形状态。
        # 到期时间每次实时判定(过期即 active=False), 不缓存。
        "guard_override": _guard_override_state(settings),
        "switches": explain_switches(
            paper_trading=bool(settings.paper_trading),
            binance_testnet=bool(settings.binance_testnet),
            live_trading_confirm=str(settings.live_trading_confirm or ""),
            mainnet_api_scope_confirmed=bool(settings.mainnet_api_scope_confirmed),
            web_admin_token_configured=bool(settings.web_admin_token),
            auth_disabled=auth_disabled,
        ),
    }
