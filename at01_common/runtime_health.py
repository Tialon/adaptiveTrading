"""运行时健康快照 + 状态分类(V11.5 P1-1)

把分散在 run.py / 各引擎里的「系统当前能不能交易、为什么不能」统一收口到一个
`/api/metrics` 里可读的 health 快照, 并提供单一的状态分类器(SAFE / DEGRADED /
REDUCE_ONLY / PAUSED / RECOVERY / KILLED / TRADING), 供监控面板 / Dashboard 直接显示。

为什么需要: 此前「系统是否健康」要同时看生命周期态、风险态、急停开关、熔断器、
统一闸门(连接/行情/交易所/对账)、后台任务、指标告警 7 处, 彼此割裂; 面板无法用
一个数字/一个词判断「现在能不能交易、为什么不能」。本模块把它们聚合为单一快照,
并用确定性优先级归并出单一运行状态。

设计:
- `build_runtime_health(state)`: 从 `web_state.system_state` 聚合各引擎健康信号,
  纯读、无副作用、对缺失句柄容错(未初始化返回「未运行」快照, 不抛异常)。
- `classify_runtime_status(health)`: 纯函数, 把快照里的多维权衡为单一状态。
  判定优先级(最严重优先): KILLED > RECOVERY > PAUSED > REDUCE_ONLY > DEGRADED
  > TRADING > SAFE。

状态语义(与 production-readiness 对齐):
- KILLED       冻结: 急停 / SAFE_MODE / 停止 / 风险 KILLED(需人工恢复, 不可交易)。
- RECOVERY     恢复中: 风险 RECOVERY_CHECK / 生命周期 RECOVERY(不可交易)。
- PAUSED       暂停: 风险 PAUSED / 熔断器打开 / 资金熔断 PAUSE|KILL(不可开新仓)。
- REDUCE_ONLY  仅减仓: 风险 REDUCE_ONLY / 资金熔断 REDUCE_ONLY(可减仓不可开仓)。
- DEGRADED     降级: 生命周期 DEGRADED(禁开仓, 可安全离场)。
- TRADING      交易中: 生命周期 TRADING 且无任何阻断。
- SAFE         就绪/安全: 生命周期 READY 或启动早期, 无风险(尚未开始交易)。
"""

import time
from typing import Any

# 状态分类词汇表(单一来源, 供面板/告警/测试引用)
STATUS_KILLED = "KILLED"
STATUS_RECOVERY = "RECOVERY"
STATUS_PAUSED = "PAUSED"
STATUS_REDUCE_ONLY = "REDUCE_ONLY"
STATUS_DEGRADED = "DEGRADED"
STATUS_TRADING = "TRADING"
STATUS_SAFE = "SAFE"


def classify_runtime_status(health: dict[str, Any]) -> str:
    """把健康快照分类为单一运行状态(纯函数, 优先级见模块 docstring)。

    对缺失/占位键容错(缺省视为「无风险」), 便于未初始化快照也得到确定性结果。
    """
    kill = bool((health.get("kill_switch") or {}).get("armed"))
    lifecycle = (health.get("lifecycle") or {}).get("state", "")
    risk = (health.get("risk") or {}).get("state", "")
    breaker_open = bool((health.get("breaker") or {}).get("open"))
    fund_action = (health.get("breaker") or {}).get("fund_action", "NONE")

    # 1. 冻结(急停 / 安全模式 / 停止 / 风险急停)
    if kill or lifecycle in ("SAFE_MODE", "STOPPED") or risk == "KILLED":
        return STATUS_KILLED
    # 2. 恢复核验 / 恢复中
    if risk == "RECOVERY_CHECK" or lifecycle == "RECOVERY":
        return STATUS_RECOVERY
    # 3. 暂停(风险暂停 / 熔断器打开 / 资金熔断 PAUSE|KILL)
    if risk == "PAUSED" or breaker_open or fund_action in ("PAUSE", "KILL"):
        return STATUS_PAUSED
    # 4. 仅减仓
    if risk == "REDUCE_ONLY" or fund_action == "REDUCE_ONLY":
        return STATUS_REDUCE_ONLY
    # 5. 降级
    if lifecycle == "DEGRADED":
        return STATUS_DEGRADED
    # 6. 交易中
    if lifecycle == "TRADING":
        return STATUS_TRADING
    return STATUS_SAFE


def _task_snapshot(supervisor: Any) -> dict[str, Any]:
    """后台任务聚合(总数/运行中/失败数/累计失败/运行中任务名)。"""
    if supervisor is None:
        return {"total": 0, "running": 0, "failed": 0, "failure_count": 0, "active": []}
    statuses = supervisor.status() if supervisor is not None else []
    active: list[str] = []
    failed = 0
    running_count = 0
    for s in statuses:
        if s.get("running"):
            running_count += 1
            active.append(s.get("name"))
        if s.get("exception"):
            failed += 1
    return {
        "total": len(statuses),
        "running": running_count,
        "failed": failed,
        "failure_count": getattr(supervisor, "failure_count", failed),
        "active": active,
    }


def build_runtime_health(state: Any) -> dict[str, Any]:
    """从 `web_state.system_state` 聚合统一健康快照(对缺失句柄容错, 不抛异常)。

    `state` 为 `SystemState` 实例(或任何具有相同字段的对象); 字段缺失/None 时
    给出「未初始化」占位, 保证 /api/metrics 在系统尚未完全启动时也可读。
    """
    started_at = getattr(state, "started_at", None)
    running = bool(getattr(state, "running", False))

    rm = getattr(state, "risk_manager", None)
    lc = getattr(state, "lifecycle", None)
    gate = getattr(state, "trading_gate", None)
    metrics = getattr(state, "metrics", None)
    supervisor = getattr(state, "supervisor", None)
    market = getattr(state, "market_engine", None)

    lifecycle = {
        "state": lc.current if lc is not None else "未初始化",
        "reason": (lc.reason if lc is not None else "") or "",
        "last_transition_at": lc.last_transition_at if lc is not None else None,
    }

    risk = {
        "state": rm.state_machine.current if rm is not None else "未初始化",
        "reason": (rm.state_machine.reason if rm is not None else "") or "",
    }

    kill_switch = {
        "armed": bool(rm.kill_switch.is_armed) if rm is not None else False,
        "reason": (rm.kill_switch.reason if rm is not None else "") or "",
    }

    breaker = {
        "open": bool(rm.breaker.is_open) if rm is not None else False,
        "reason": (rm.breaker.reason if rm is not None else "") or "",
        "fund_action": gate.last_breaker_action.value if gate is not None else "NONE",
    }

    reconcile = {
        "reconciled": bool(gate.reconciled) if gate is not None else False,
        "last_reconcile_at": getattr(state, "last_reconcile_at", None),
    }

    exchange = {
        "healthy": bool(gate.exchange_healthy) if gate is not None else False,
    }

    last_prices: dict[str, float] = {}
    if market is not None:
        for s, st in getattr(market, "state", {}).items():
            last_prices[s] = getattr(st, "last_price", 0.0)
    market_health = {
        "connection_ok": bool(gate.connection_ok) if gate is not None else False,
        "data_healthy": bool(gate.market_data_healthy) if gate is not None else False,
        "last_prices": last_prices,
        "ws_silence_seconds": round((rm.ws_silence_seconds if rm is not None else 0.0) or 0.0, 2),
    }

    trade = {
        "orders_total": metrics.get_counter("orders_total") if metrics is not None else 0,
        "orders_failed": metrics.get_counter("orders_failed") if metrics is not None else 0,
        "order_failure_rate": round(metrics.order_failure_rate(), 4)
        if metrics is not None
        else 0.0,
    }

    last_event = None
    if lc is not None and lc.history:
        last_event = lc.history[-1]

    # V11.6 P1-2: 交易许可契约 —— can_buy/can_sell 直接取自统一闸门(单一权威),
    # 保证 health.can_buy == TradingGate.can_open_position()[0](而非另起一套判定)。
    # 闸门未就绪(未注入 / 假句柄无该方法)时保守拒绝, 不虚报「可买」。
    if gate is not None and callable(getattr(gate, "can_open_position", None)):
        can_buy, buy_block_reason = gate.can_open_position()
        can_sell, sell_block_reason = gate.can_reduce_position()
    else:
        can_buy = False
        buy_block_reason = "交易闸门未就绪"
        can_sell = False
        sell_block_reason = "交易闸门未就绪"

    health: dict[str, Any] = {
        "status": "",
        "running": running,
        "uptime_seconds": round(time.time() - started_at, 2) if started_at else 0.0,
        "lifecycle": lifecycle,
        "risk": risk,
        "kill_switch": kill_switch,
        "breaker": breaker,
        "reconcile": reconcile,
        "market": market_health,
        "exchange": exchange,
        "tasks": _task_snapshot(supervisor),
        "trade": trade,
        "can_buy": can_buy,
        "can_sell": can_sell,
        "buy_block_reason": buy_block_reason,
        "sell_block_reason": sell_block_reason,
        "last_event": last_event,
        "last_error": getattr(state, "last_error", None),
    }
    health["status"] = classify_runtime_status(health)
    # V11.6 P1-2: 状态契约 —— `state` 为规范化状态名(与 `status` 同值, 保留旧键兼容面板)
    health["state"] = health["status"]
    return health
