"""一键进入无人值守(V13 P0)—— 首次配置向导 + 就绪核对。

**这个模块解决什么**: 此前要跑起来, 操作者得先读懂 `PAPER_TRADING` / `BINANCE_TESTNET` /
`RUN_TESTNET_TRADING` / `LIVE_TRADING_CONFIRM` / `MAINNET_API_SCOPE_CONFIRMED` 这一组布尔量的
组合关系, 再自己去 `/admin` 找对应字段。任务书要把这一步压成**五件事**:

    ① API Key / Secret 已配置
    ② 交易模式
    ③ 交易资金范围 / 风控边界
    ④ 策略版本
    ⑤ 是否允许进入真实资金运行

除此之外的事情(启动自检、对账、重连、恢复)全部由系统自己做。

**边界(不可越界)**
- 本模块**只读**。它不解锁任何守卫、不写任何配置 —— 写走**既有**通道:
  模式 `POST /api/trading-mode/preview|apply`, 参数 `POST /api/admin/config/*`。
  另起一套写路径等于在守卫旁边开一个旁门。
- 高风险项(进入实盘 / 改核心风控参数 / 激活策略版本)在向导里**不可绕过**:
  向导只负责告诉用户「还差什么」, 真正落地的动作仍走它们各自的确认流程。
- 结论仍取自 `build_operator_status`(其 `can_buy`/`can_sell` 又直接取自 `TradingGate`),
  本模块不重判交易许可。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from at01_common.settings import get_settings

setup_router = APIRouter()

# 向导展示的风控边界(任务书 ③)。键与 `config_store.FieldSpec` 一致, 复用其中文标签与单位。
RISK_BOUNDARY_KEYS: tuple[str, ...] = (
    "RISK_MAX_POSITION_PCT",
    "RISK_MAX_SINGLE_ORDER_PCT",
    "RISK_MAX_SOL_EXPOSURE",
    "RISK_MAX_DAILY_LOSS",
    "RISK_MAX_DRAWDOWN",
)

# 需要**风险提示**的档位: 到了这些值, 一次不利行情就可能触及日内亏损熔断。
_AGGRESSIVE_DAILY_LOSS = 0.05



def _step(
    key: str, label: str, done: bool, *, detail: str = "", action: str = "",
    value: str = "", required: bool = True, rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "key": key, "label": label, "done": bool(done), "required": required,
        "value": value, "detail": detail, "action": "" if done else action,
        "rows": rows or [],
    }


def _credentials_step(settings: Any, mode: str) -> dict[str, Any]:
    """① API Key / Secret —— 按**当前模式**判定需要哪一组凭证。

    模拟模式不需要任何密钥(不连交易所私有接口), 所以这一步在模拟下天然就绪 ——
    不该让一个只想跑模拟的人先去申请 API Key。
    """
    if mode == "paper":
        return _step(
            "credentials", "API Key / Secret", True,
            value="不需要", detail="模拟模式只读公开行情, 不连接交易所私有接口。",
        )
    if mode == "testnet":
        has = bool(settings.binance_testnet_api_key and settings.binance_testnet_api_secret)
        return _step(
            "credentials", "API Key / Secret", has,
            value="已配置" if has else "未配置",
            detail="测试模式使用 Binance 测试网密钥。",
            action="在部署配置里填写 BINANCE_TESTNET_API_KEY / BINANCE_TESTNET_API_SECRET"
                   "(密钥只存在于配置文件, 不会进入数据库或页面)。",
        )
    has = bool(settings.binance_api_key and settings.binance_api_secret)
    return _step(
        "credentials", "API Key / Secret", has,
        value="已配置" if has else "未配置",
        detail="实盘模式使用 Binance 主网密钥, 权限应仅限现货交易并关闭提现。",
        action="在部署配置里填写 BINANCE_API_KEY / BINANCE_API_SECRET, "
               "并在 Binance 后台关闭提现与资金划转权限。",
    )


def _mode_step(mode: str, mode_label: str, resolved: Any) -> dict[str, Any]:
    """② 交易模式 —— 三选一。已解析出模式即为完成。"""
    return _step(
        "mode", "交易模式", bool(mode), value=mode_label,
        detail="模拟 = 本地模拟成交; 测试 = Binance 测试网真实下单(假钱); "
               "实盘 = Binance 主网真实资金。",
        action="在「运行模式」里三选一。"
               + (f" 当前配置有问题: {resolved.error}" if getattr(resolved, "error", "") else ""),
    )


def _risk_step(settings: Any) -> dict[str, Any]:
    """③ 资金与风控边界 —— 只展示**关键几项**, 不是把几十个参数摊给用户填。"""
    from at01_common.config_store import SPECS_BY_KEY

    rows: list[dict[str, Any]] = []
    warn = ""
    for key in RISK_BOUNDARY_KEYS:
        spec = SPECS_BY_KEY.get(key)
        if spec is None:
            continue
        value = getattr(settings, spec.attr, None)
        shown = round(float(value) * 100, 2) if spec.kind == "pct" and value is not None else value
        rows.append({"key": key, "label": spec.label, "value": shown, "unit": spec.unit})
        if key == "RISK_MAX_DAILY_LOSS" and isinstance(shown, (int, float)):
            if float(shown) / 100 > _AGGRESSIVE_DAILY_LOSS:
                warn = f"日内亏损阈值 {shown}% 偏宽, 一次不利行情就可能触及熔断。"
    return _step(
        "risk", "资金与风控边界", bool(rows), value="已设定",
        detail=warn or "系统已给出一组推荐默认值, 通常无需修改。",
        rows=rows,
    )


def _strategy_step(version: str, note: str = "", *, activated: bool = False) -> dict[str, Any]:
    """④ 策略版本 —— 系统每次启动会冻结一份基线版本, 所以这一步正常都是就绪的。

    **为什么把「启动基线」也算就绪**: `StrategyVersionManager.snapshot()` 建的基线是
    `active=False` 的 —— 它记录的是「服务启动那一刻的参数快照」, 而不是「人工激活过的版本」。
    若只认 `active=True`, 向导会显示「策略版本: 无」, 而系统其实正带着一组明确、可追溯的参数
    在跑。那会让人以为出了故障并去点不该点的东西。

    所以这里如实区分两种: 已激活的版本 vs 启动基线, 文案上写清楚是哪一种。
    """
    if not version:
        return _step(
            "strategy", "策略版本", False, value="无",
            detail="尚未生成任何策略版本快照。",
            action="重启服务以生成启动基线版本。",
        )
    kind = "已激活版本" if activated else "启动基线(未人工激活)"
    # 系统自己写的 note 是机器标识(英文), 对用户没有信息量 ——
    # 只有**人工写下的**备注才值得展示, 否则用我们的中文解释。
    from at30_strategy.strategy_version import is_auto_note

    human_note = "" if is_auto_note(note) else (note or "").strip()
    detail = human_note or (
        "启动时自动冻结了当前策略参数, 用于追溯「这个收益是哪套参数跑出来的」。"
        "AI 优化器只会产出**建议**(proposal), 需人工确认后才会成为新版本。"
    )
    return _step(
        "strategy", "策略版本", True, value=f"{version} · {kind}", detail=detail,
    )


def _live_confirm_step(settings: Any, mode: str) -> dict[str, Any]:
    """⑤ 真实资金确认 —— 只在实盘模式下**必须**; 其余模式标记为「不适用」。"""
    if mode != "live":
        return _step(
            "live_confirm", "真实资金运行确认", True, required=False,
            value="不适用", detail="当前不是实盘模式, 不涉及真实资金。",
        )
    confirmed = (
        str(getattr(settings, "live_trading_confirm", "")).strip().lower() == "true"
        and bool(getattr(settings, "mainnet_api_scope_confirmed", False))
    )
    return _step(
        "live_confirm", "真实资金运行确认", confirmed,
        value="已确认" if confirmed else "未确认",
        detail="实盘会动用真实资金; 该确认必须是一次**人工动作**, 系统不会代填。",
        action="在「运行模式」里选择实盘, 并在确认弹窗中核对风控参数后确认。",
    )


def build_setup_status(
    settings: Any, *, active_version: str = "", resolved: Any = None,
    status: dict[str, Any] | None = None, version_activated: bool = False, note: str = "",
) -> dict[str, Any]:
    """五要素核对 + 无人值守就绪结论(纯函数, 便于独立测试)。

    任务书要求: 「系统启动 → 自动检查 → 自动修复可修复问题 → 无法自动解决才告诉用户原因
    → 只有高风险事项才要求人工确认 → READY → 无人值守运行」。
    因此这里返回的 `ready` 只表示**五件人工事项已齐备**, 不代表系统健康 ——
    系统健康由 `/api/operator-status` 回答。
    """
    from at01_common.trading_mode import MODE_LABELS, TradingMode, resolve_mode

    if resolved is None:
        resolved = resolve_mode(
            trading_mode=getattr(settings, "trading_mode", ""),
            paper_trading=bool(getattr(settings, "paper_trading", True)),
            binance_testnet=bool(getattr(settings, "binance_testnet", True)),
            run_testnet_trading=getattr(settings, "run_testnet_trading", ""),
            live_trading_confirm=getattr(settings, "live_trading_confirm", ""),
            mainnet_api_scope_confirmed=bool(getattr(settings, "mainnet_api_scope_confirmed", False)),
            explicitly_set=set(getattr(settings, "model_fields_set", set())),
        )
    mode = resolved.mode.value if resolved.mode else ""
    mode_label = MODE_LABELS.get(resolved.mode, "未知") if resolved.mode else "未确定"

    steps = [
        _credentials_step(settings, mode),
        _mode_step(mode, mode_label, resolved),
        _risk_step(settings),
        _strategy_step(active_version, note, activated=version_activated),
        _live_confirm_step(settings, mode),
    ]
    missing = [s for s in steps if s["required"] and not s["done"]]
    ready = not missing and resolved.mode is not None

    conclusion = {
        "ready": ready,
        "title": "系统已进入无人值守运行" if ready else "还差几项配置",
        "summary": (
            "系统会自动处理普通异常(重连 / 重试 / 对账 / 恢复), 需要你时会在首页「最近需要你关注的事情」列出。"
            if ready else
            "请完成下列配置; 完成后系统即可无人值守运行。"
        ),
        "missing": [s["label"] for s in missing],
    }

    return {
        "ready": ready,
        "conclusion": conclusion,
        "steps": steps,
        "mode": mode,
        "mode_label": mode_label,
        "market_data_source": resolved.market_data_source.value,
        "mode_error": resolved.error,
        "symbol": (getattr(settings, "symbol_list", []) or [""])[0],
        # 三模式的可选性(写入走既有 /api/trading-mode/*), 供向导直接渲染三张卡
        "modes": [
            {"id": m.value, "label": MODE_LABELS[m], "is_current": m is resolved.mode}
            for m in (TradingMode.PAPER, TradingMode.TESTNET, TradingMode.LIVE)
        ],
        "status": status or {},
    }


async def _current_strategy_version() -> tuple[str, str, bool]:
    """当前策略版本(供 ④)。返回 `(version, note, activated)`。

    优先取 `active=True`(人工激活过的); 没有则回落到最新一条(即启动基线), 并把
    `activated=False` 如实带出去 —— 向导据此区分「已激活版本」与「启动基线」。
    都读不到就返回空 —— **不能编一个版本号出来**。
    """
    try:
        from sqlalchemy import select

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import StrategyVersion

        async with AsyncSessionLocal() as session:
            activated_row = (
                (
                    await session.execute(
                        select(StrategyVersion)
                        .where(StrategyVersion.active.is_(True))
                        .order_by(StrategyVersion.id.desc())
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
            if activated_row is not None:
                return str(activated_row.version), str(activated_row.note or ""), True
            latest_row = (
                (
                    await session.execute(
                        select(StrategyVersion).order_by(StrategyVersion.id.desc()).limit(1)
                    )
                )
                .scalars()
                .first()
            )
            if latest_row is not None:
                return str(latest_row.version), str(latest_row.note or ""), False
    except Exception:
        pass
    return "", "", False


@setup_router.get("/setup", response_class=HTMLResponse)
async def setup_page() -> HTMLResponse:
    """一键进入无人值守的配置向导页(只读展示; 写操作走既有通道)。"""
    from pathlib import Path

    html = (Path(__file__).parent / "static" / "setup.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@setup_router.get("/api/setup/status")
async def setup_status() -> dict[str, Any]:
    """五要素核对(只读, 无需令牌)。**不含任何写路径** —— 写入仍走既有通道。"""
    from at01_common.runtime_health import build_runtime_health
    from at90_web.web_operator_status import build_operator_status
    from at90_web.web_state import system_state
    from at90_web.web_status_collect import collect_extras

    version, note, activated = await _current_strategy_version()
    try:
        health = build_runtime_health(system_state)
    except Exception:
        health = {}
    try:
        # 复用首屏那套探测(权益/今日统计等), 免得向导里的「账户权益」永远是空 ——
        # 单一来源, 两处显示才不会打架。
        extras = await collect_extras(system_state)
    except Exception:
        extras = {}
    try:
        status = build_operator_status(get_settings(), health, extras=extras)
    except Exception:
        status = {}
    body = build_setup_status(
        get_settings(), active_version=version, status=status,
        version_activated=activated, note=note,
    )
    body["strategy_note"] = note
    body["strategy_activated"] = activated
    # 无人值守结论卡要展示的现场事实, 直接复用首屏那套(单一来源, 不另算一遍)
    body["unattended"] = {
        "mode_label": status.get("trading_mode_label", body["mode_label"]),
        "symbol": body["symbol"],
        "equity": status.get("equity"),
        "risk_level": status.get("risk_level"),
        "can_buy": status.get("can_buy"),
        "can_sell": status.get("can_sell"),
        "reconciled": (status.get("runtime") or {}).get("reconciled"),
        "health": status.get("health_summary", {}),
        "strategy_version": version,
    }
    return body
