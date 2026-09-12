"""系统健康报告(V13 P1) —— `/ops` 从「部署考试页面」变成「系统给用户看的健康报告」。

**这个模块解决什么**: 此前 `/ops` 输出的是一张 PASS / WARN / BLOCKED 检查表, 隐含的
使用方式是「用户逐项看、自己判断、自己处理」。任务书要求改成结论优先的健康报告:
**先给结论, 只有异常项才展开技术细节**, 并且每项都标明「这件事归谁管」。

**处理方式三分类**(任务书 P0 的 A / B / C), 每项必带 `handling`:

    AUTO        系统自动处理 —— 重连/重试/退避/降级/恢复。**不打扰用户。**
    AUTO_BLOCK  系统自动阻止交易, 但服务继续运行。只展示原因, **无需人工确认**。
    HUMAN       必须人工确认 —— 进主网真钱 / 改核心风控参数 / 激活策略版本 /
                解除高风险 KILL / 改资金规模 / 重大资金异常后重启。

把三分类收口在**这一处**, 页面就不必各自判断「要不要打扰用户」——
这是任务书「结论 > 原因 > 影响 > 系统动作 > 用户动作」能否一致落地的关键。

**边界**: 本模块只读, 不实现任何交易判定; `can_buy`/`can_sell` 仍直接取 `runtime_health`
(其本身取自 `TradingGate`)。这里只是把既有信号分组、翻译、分类。
"""

from __future__ import annotations

import shutil
from typing import Any

# 处理方式(单一来源)
HANDLING_AUTO = "AUTO"
HANDLING_AUTO_BLOCK = "AUTO_BLOCK"
HANDLING_HUMAN = "HUMAN"

HANDLING_LABELS: dict[str, str] = {
    HANDLING_AUTO: "系统自动处理",
    HANDLING_AUTO_BLOCK: "系统自动阻止交易",
    HANDLING_HUMAN: "需要你的确认",
}


def _item(
    key: str, label: str, ok: bool, conclusion: str, *,
    handling: str = HANDLING_AUTO, detail: str = "", human_action: str = "",
    blocking: bool = True,
) -> dict[str, Any]:
    """一个检查项。

    `blocking` 区分「**拦得住交易**的问题」与「**只是观察指标**」。默认 True 是刻意的
    fail-closed: 没标注的项都按真问题算, 免得将来新增项时忘了标而静默降级。

    为什么需要它: 订单质量是**喂给 AI 复盘**的观察项 —— 一笔订单失败就是没成交而已,
    系统不会因此受限, 用户也无事可做。若把它算进总结, 页面就会在一切正常时显示
    「系统正在自动处理」, 那是**虚报异常**, 与「无事就明说无需操作」直接冲突。
    """
    return {
        "key": key,
        "label": label,
        "ok": bool(ok),
        "conclusion": conclusion,
        "handling": handling,
        "handling_label": HANDLING_LABELS[handling],
        "detail": detail,          # 只在异常时页面才展开
        "human_action": human_action,
        "blocking": bool(blocking),
    }


def _config_item(settings: Any) -> dict[str, Any]:
    """配置项: 直接复用启动期的 `validate()` —— 与真正启动时同源, 不另起一套判定。"""
    try:
        problems = list(settings.validate())
    except Exception as exc:  # 校验本身失败也要如实报告, 不能显示成「正常」
        return _item(
            "config", "配置", False, "配置校验未能完成",
            handling=HANDLING_HUMAN, detail=str(exc),
            human_action="检查配置文件的合法性后重新启动。",
        )
    if problems:
        return _item(
            "config", "配置", False, f"有 {len(problems)} 项配置问题",
            handling=HANDLING_HUMAN, detail="; ".join(problems[:5]),
            human_action="修正这些配置项后重新启动。",
        )
    return _item("config", "配置", True, "配置正常")


def _database_item(db_ok: bool | None, persist_failures: int) -> dict[str, Any]:
    """数据库项。`db_ok=None` 表示未探测(纯函数调用路径) —— 如实说「未探测」而非假装正常。"""
    if db_ok is None:
        if persist_failures:
            return _item(
                "database", "数据库", False, "事件写入出现失败",
                handling=HANDLING_AUTO_BLOCK,
                detail=f"累计 {persist_failures} 次事件落库失败",
                human_action="检查数据库连接与磁盘空间。",
            )
        return _item("database", "数据库", True, "正常(未单独探测)")
    if not db_ok:
        return _item(
            "database", "数据库", False, "数据库不可达",
            handling=HANDLING_AUTO_BLOCK,
            detail="连通性探测失败",
            human_action="检查数据库服务与连接配置。",
        )
    return _item("database", "数据库", True, "正常")


def build_health_report(
    health: dict[str, Any],
    *,
    settings: Any = None,
    db_ok: bool | None = None,
    disk: dict[str, Any] | None = None,
    persist_failures: int = 0,
) -> list[dict[str, Any]]:
    """把健康快照分组为「用户看得懂的检查项」(每项带处理方式)。

    纯函数(磁盘/数据库探测结果由调用方传入), 对缺失字段容错。
    """
    health = health or {}
    lifecycle = health.get("lifecycle") or {}
    risk = health.get("risk") or {}
    kill = health.get("kill_switch") or {}
    breaker = health.get("breaker") or {}
    reconcile = health.get("reconcile") or {}
    market = health.get("market") or {}
    exchange = health.get("exchange") or {}
    tasks = health.get("tasks") or {}
    trade = health.get("trade") or {}

    items: list[dict[str, Any]] = []

    if settings is not None:
        items.append(_config_item(settings))
    items.append(_database_item(db_ok, persist_failures))

    # Exchange / 行情 / 对账 / 风控 / 执行 —— 异常时属「系统自动阻止交易」类:
    # 系统会自己停机并重试, 用户不需要做任何事(除非持续不恢复)。
    exchange_ok = bool(exchange.get("healthy"))
    items.append(_item(
        "exchange", "Binance", exchange_ok,
        "正常" if exchange_ok else "连接或对账链路异常",
        handling=HANDLING_AUTO_BLOCK,
        detail="" if exchange_ok else "交易所接口当前不可信, 系统已暂停下单",
        human_action="通常无需操作; 持续不恢复时检查网络与 API 状态。",
    ))

    market_ok = bool(market.get("data_healthy")) and bool(market.get("connection_ok"))
    silence = float(market.get("ws_silence_seconds") or 0.0)
    items.append(_item(
        "market", "行情", market_ok,
        "正常" if market_ok else "行情未就绪或已静默",
        handling=HANDLING_AUTO_BLOCK,
        detail="" if market_ok else f"行情静默 {silence:.0f}s, 系统正在自动重连",
        human_action="无需操作, 系统会自动重连。",
    ))

    reconciled = bool(reconcile.get("reconciled"))
    items.append(_item(
        "reconcile", "对账", reconciled,
        "正常" if reconciled else "尚未对齐",
        handling=HANDLING_AUTO_BLOCK,
        detail="" if reconciled else "账户与系统账本存在差异或尚未完成对账",
        human_action="无需操作, 系统会持续自动对账; 持续无法对齐会来通知你。",
    ))

    risk_state = str(risk.get("state") or "")
    fund_action = str(breaker.get("fund_action") or "NONE")
    risk_ok = risk_state in ("NORMAL", "") and fund_action == "NONE"
    # 资金熔断 KILL 属重大资金异常 → HUMAN; 其余风险档位系统会自己恢复
    risk_handling = (
        HANDLING_HUMAN if (fund_action == "KILL" or kill.get("armed"))
        else HANDLING_AUTO_BLOCK
    )
    items.append(_item(
        "risk", "风控", risk_ok,
        "正常" if risk_ok else "已收紧交易",
        handling=risk_handling,
        detail="" if risk_ok else f"风险档位 {risk_state or '未知'} / 熔断 {fund_action}",
        human_action=(
            "" if risk_ok else
            "核对 Binance 账户资产与系统账本一致后, 再执行「恢复急停」。"
            if risk_handling == HANDLING_HUMAN else
            "无需操作, 系统会自动恢复。"
        ),
    ))

    failed_tasks = int(tasks.get("failed") or 0)
    items.append(_item(
        "tasks", "后台任务", failed_tasks == 0,
        "正常" if failed_tasks == 0 else f"{failed_tasks} 个任务异常",
        handling=HANDLING_AUTO_BLOCK,
        detail="" if failed_tasks == 0 else f"运行中 {tasks.get('running')}/{tasks.get('total')}",
        human_action="无需操作, 系统会自动重启失败的任务; 反复失败时会来通知你。",
    ))

    exec_ok = lifecycle.get("state") not in ("STOPPED", None, "")
    items.append(_item(
        "execution", "执行", exec_ok,
        "正常" if exec_ok else "已停止",
        handling=HANDLING_AUTO if exec_ok else HANDLING_HUMAN,
        detail="" if exec_ok else "服务已停止, 不会再发出任何订单",
        human_action="" if exec_ok else "重新启动服务即可恢复。",
    ))

    # 订单失败率: **观察项, 不阻断** —— 失败的订单本就不会成交, 用户无事可做。
    # 见 `_item` 里对 `blocking` 的说明。
    rate = float(trade.get("order_failure_rate") or 0.0)
    items.append(_item(
        "orders", "订单质量", rate < 0.5,
        "正常" if rate < 0.5 else "失败率偏高",
        handling=HANDLING_AUTO, blocking=False,
        detail=f"订单 {trade.get('orders_total', 0)} 笔, 失败率 {rate:.1%}",
        human_action="",  # 这是给 AI 复盘的输入, 不是给人派活
    ))

    # V14 §9: Redis 依赖项。**OPTIONAL** —— 但不可用时必须**显式**记为降级,
    # 不能因为「不影响交易」就让用户以为一切如常。
    deps = health.get("dependencies") or {}
    redis = deps.get("redis") or {}
    if redis.get("enabled"):
        connected = bool(redis.get("connected"))
        items.append(_item(
            "redis", "事件总线(Redis)", connected,
            "正常" if connected else "已降级运行",
            handling=HANDLING_AUTO, blocking=False,   # 不阻断结论, 但如实出现在列表里
            detail="" if connected else (
                "Redis 未连接, 事件总线旁路不可用。"
                "**交易不受影响**(主链路走内存回调, Redis 只承载一条无消费方的事件流)。"
                + (f" 原因: {redis.get('error')}" if redis.get("error") else "")
            ),
            human_action="" if connected else "无需操作; 如需该旁路, 检查 Redis 服务与连接配置。",
        ))

    if disk is not None:
        free_ratio = float(disk.get("free_ratio") or 0.0)
        ok = free_ratio >= 0.05
        items.append(_item(
            "disk", "磁盘", ok,
            "正常" if ok else "剩余空间不足",
            handling=HANDLING_AUTO_BLOCK if ok else HANDLING_HUMAN,
            detail=f"剩余 {disk.get('free_gb', 0):.1f} GB ({free_ratio:.1%})",
            human_action="" if ok else "清理磁盘空间, 否则数据库写入会失败。",
        ))

    return items


def summarize_health_report(items: list[dict[str, Any]]) -> dict[str, Any]:
    """给出一句话结论 —— 页面第一行读它, 所以必须能独立成立。

    只有**需要人**的异常才升级为「需要你的确认」; 系统自己能处理的异常不该打扰用户。

    只统计 `blocking` 项: 纯观察指标(如订单失败率)不该把「一切正常」说成「正在自动处理」——
    无事就明说无需操作, 是任务书的硬要求。
    """
    bad = [i for i in items if not i["ok"] and i.get("blocking", True)]
    human = [i for i in bad if i["handling"] == HANDLING_HUMAN]
    auto = [i for i in bad if i["handling"] != HANDLING_HUMAN]

    # V14 §9: 「不阻断但确实降级」的项单独列出。它们不该让结论变成「系统不正常」,
    # 但也不能被藏起来 —— 用户有权知道自己少了一条旁路。
    degraded = [i for i in items if not i["ok"] and not i.get("blocking", True)]

    if not bad:
        return {
            "ok": True,
            "conclusion": (
                "系统正常, 无需操作。" if not degraded
                else f"系统正常, 无需操作(有 {len(degraded)} 项降级: "
                     + "、".join(i["label"] for i in degraded) + ")。"
            ),
            "needs_human": False,
            "abnormal": [],
            "degradations": [i["label"] for i in degraded],
        }
    if human:
        return {
            "ok": False,
            "conclusion": "需要你的确认: " + "、".join(i["label"] for i in human),
            "needs_human": True,
            "abnormal": [i["label"] for i in bad],
            "degradations": [i["label"] for i in degraded],
        }
    return {
        "ok": False,
        "conclusion": "系统正在自动处理: " + "、".join(i["label"] for i in auto),
        "needs_human": False,
        "abnormal": [i["label"] for i in bad],
        "degradations": [i["label"] for i in degraded],
    }


def probe_disk(path: str = ".") -> dict[str, Any] | None:
    """探测磁盘剩余空间(失败返回 None —— 如实说「未探测」, 不假装正常)。"""
    try:
        usage = shutil.disk_usage(path)
        return {
            "total_gb": usage.total / 1024 ** 3,
            "free_gb": usage.free / 1024 ** 3,
            "free_ratio": usage.free / usage.total if usage.total else 0.0,
        }
    except Exception:
        return None
