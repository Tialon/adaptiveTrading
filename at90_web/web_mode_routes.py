"""运行模式切换 API(V12.7)

操作者只面对三个模式: 模拟 / 测试网 / 实盘。底层 `PAPER_TRADING` /
`BINANCE_TESTNET` / ... 不再作为操作入口。

    GET  /api/trading-mode          当前模式 + 三个可选模式及可启动性(只读)
    POST /api/trading-mode/preview  生成切换 diff + **守卫预检**, 不写任何配置
    POST /api/trading-mode/apply    保存切换(**不重启**)

设计要点(对应任务单):

- **§9/§10** 切换走「选择 → 生成 diff → 显示 → 确认 → 保存 → 要求重启」, 不一步到位。
- **§11** 实盘需要额外显式确认, 确认页展示真实资金相关的风控参数。
- **§19** 写接口**复用现有** admin 写鉴权(`require_admin`), 不另起一套权限。
- **§20** `apply` 只保存配置, **不自己重启进程**。
- **§21** 重启后由正常启动链路完成验证; 成败经 `/api/operator-status` 呈现。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends

from at01_common.config_store import SPECS_BY_KEY
from at01_common.settings import Settings, get_settings
from at01_common.trading_mode import (
    MODE_DERIVED,
    MODE_DESCRIPTIONS,
    MODE_LABELS,
    MarketDataSource,
    TradingMode,
    resolve_mode,
)
from at90_web.web_admin_routes import _restart_hint
from at90_web.web_auth import require_admin

mode_router = APIRouter()

MODE_ORDER: tuple[TradingMode, ...] = (
    TradingMode.PAPER, TradingMode.TESTNET, TradingMode.LIVE,
)

# 实盘确认页要展示的真实资金相关参数(§11)
LIVE_RISK_KEYS: tuple[str, ...] = (
    "RISK_MAX_POSITION_PCT", "RISK_MAX_SINGLE_ORDER_PCT", "RISK_MAX_SOL_EXPOSURE",
    "RISK_MAX_DAILY_LOSS", "RISK_MAX_DRAWDOWN",
)


def _candidate(target: TradingMode, *, live_confirmed: bool) -> Settings:
    """构造切换后的候选配置(含模式推导), 供守卫预检 —— **不落任何盘**。

    `live_confirmed=True` 表示操作者已在确认弹窗里确认进入实盘;
    此时才把两个确认标志写进候选, 与真实 `apply` 的行为一致。
    """
    cand = get_settings().model_copy(deep=True)
    cand.trading_mode = target.value
    for key, value in MODE_DERIVED[target].items():
        setattr(cand, key, value)
    if target is TradingMode.LIVE and live_confirmed:
        cand.live_trading_confirm = "true"
        cand.mainnet_api_scope_confirmed = True
    return cand


def _guard_verdict(cand: Settings) -> dict[str, Any]:
    """跑**与启动期同源**的守卫, 给出能否启动的结论。纯只读。

    刻意不重写判定 —— 复用 `validate()` / `mainnet_blocked_reason()` /
    `mainnet_readiness_check()`, 与真正启动时读的是同一套逻辑。
    """
    from at01_common.testnet_gate import git_sha as _git_sha

    problems = cand.validate()
    blocked: list[str] = []
    # 用**真实**的 git_sha(与启动期同一来源) —— 曾用 "preview" 占位, 那会让预检
    # 比真实启动乐观: 主网自检第⑧项要 git_sha 非空, 占位串会掩盖它。
    sha = _git_sha() or cand.git_sha

    guard = cand.mainnet_blocked_reason()
    if guard:
        blocked.append(guard)

    if not cand.binance_testnet and not cand.paper_trading:
        from at01_common.mainnet_readiness import mainnet_readiness_check

        readiness = mainnet_readiness_check(
            binance_testnet=cand.binance_testnet,
            paper_trading=cand.paper_trading,
            live_trading_confirm=cand.live_trading_confirm,
            api_scope_confirmed=cand.mainnet_api_scope_confirmed,
            symbol=",".join(cand.symbol_list),
            config_problems=problems,
            kill_switch_armed=False,
            git_sha=sha,
            base_url=cand.binance_rest_url,
        )
        if not readiness["allowed"]:
            blocked.extend(readiness["blocked_reasons"])

    if cand.binance_testnet and not cand.paper_trading:
        from at01_common.testnet_gate import testnet_preflight

        pre = testnet_preflight(
            binance_testnet=cand.binance_testnet,
            paper_trading=cand.paper_trading,
            live_trading=cand.live_trading_confirm.strip().lower() == "true",
            run_testnet_trading=cand.run_testnet_trading,
            credentials_present=bool(
                cand.binance_testnet_api_key and cand.binance_testnet_api_secret
            ),
            git_sha=sha,
            symbol=",".join(cand.symbol_list),
        )
        if not pre["allowed"]:
            blocked.extend(pre["blocked_reasons"])

    return {"ok": not problems and not blocked, "problems": problems, "blocked_reasons": blocked}


def _mode_changes(current: TradingMode | None, target: TradingMode) -> list[dict[str, Any]]:
    """列出这次切换会改动哪些**内部字段**(§10 的 diff)。"""
    base = get_settings()
    rows: list[dict[str, Any]] = []
    keys = set(MODE_DERIVED.get(target, {}))
    if current is not None:
        keys |= set(MODE_DERIVED.get(current, {}))
    for key in sorted(keys):
        spec = next((s for s in SPECS_BY_KEY.values() if s.attr == key), None)
        want = MODE_DERIVED[target].get(key)
        rows.append({
            "key": spec.key if spec else key,
            "attr": key,
            "label": spec.label if spec else key,
            "from": getattr(base, key, None),
            "to": want,
            "changed": getattr(base, key, None) != want,
        })
    return rows


def _risk_summary() -> list[dict[str, Any]]:
    """实盘确认页展示的风控参数(§11)。"""
    s = get_settings()
    out: list[dict[str, Any]] = []
    for key in LIVE_RISK_KEYS:
        spec = SPECS_BY_KEY.get(key)
        if spec is None:
            continue
        value = getattr(s, spec.attr, None)
        out.append({
            "key": key, "label": spec.label, "unit": spec.unit,
            "value": round(float(value) * 100, 4) if spec.kind == "pct" else value,
        })
    return out


@mode_router.get("/api/trading-mode")
async def get_trading_mode() -> dict[str, Any]:
    """当前模式 + 三个可选模式(只读, 不需令牌)。

    每个模式给 `startable` 与 `blocked_reasons` —— 让操作者在**点之前**就知道
    哪个模式起得来、起不来的原因是什么。
    """
    s = get_settings()
    r = resolve_mode(
        trading_mode=s.trading_mode, paper_trading=s.paper_trading,
        binance_testnet=s.binance_testnet, run_testnet_trading=s.run_testnet_trading,
        live_trading_confirm=s.live_trading_confirm,
        mainnet_api_scope_confirmed=s.mainnet_api_scope_confirmed,
        explicitly_set=set(s.model_fields_set),
    )

    modes: list[dict[str, Any]] = []
    for m in MODE_ORDER:
        # 实盘一律标"需确认" —— 预检时按"尚未确认"算, 免得给人"点一下就进实盘"的错觉
        verdict = _guard_verdict(_candidate(m, live_confirmed=False))
        reasons = list(verdict["blocked_reasons"])
        if m is TradingMode.LIVE and not s.live_trading_confirm.strip():
            reasons.insert(0, "实盘需要显式确认(需在确认弹窗中核对风控参数后确认)")
        modes.append({
            "id": m.value,
            "label": MODE_LABELS[m],
            "description": MODE_DESCRIPTIONS[m],
            "startable": not reasons,
            "blocked_reasons": reasons,
            "is_current": r.mode is m,
        })

    return {
        "current": {
            "mode": r.mode.value if r.mode else "",
            "label": r.label,
            "market_data_source": r.market_data_source.value,
            "source": r.source,
            "error": r.error,
        },
        "modes": modes,
        "symbol": s.symbol_list[0] if s.symbol_list else "",
    }


@mode_router.post("/api/trading-mode/preview", dependencies=[Depends(require_admin)])
async def preview_trading_mode(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """生成切换 diff + 守卫预检。**不写任何配置。**

    `{ "mode": "testnet", "live_confirmed": false }`
    """
    raw = str(payload.get("mode") or "").strip().lower()
    try:
        target = TradingMode(raw)
    except ValueError:
        return {"ok": False, "error": f"未知模式: {raw!r}; 只接受 paper/testnet/live"}

    s = get_settings()
    r = resolve_mode(
        trading_mode=s.trading_mode, paper_trading=s.paper_trading,
        binance_testnet=s.binance_testnet, run_testnet_trading=s.run_testnet_trading,
        live_trading_confirm=s.live_trading_confirm,
        mainnet_api_scope_confirmed=s.mainnet_api_scope_confirmed,
        explicitly_set=set(s.model_fields_set),
    )
    live_confirmed = bool(payload.get("live_confirmed"))

    cand = _candidate(target, live_confirmed=live_confirmed)
    verdict = _guard_verdict(cand)

    needs_live_confirm = target is TradingMode.LIVE and not live_confirmed
    if needs_live_confirm:
        verdict = {
            "ok": False,
            "problems": verdict["problems"],
            "blocked_reasons": ["尚未确认进入实盘(见下面的真实资金参数)"],
        }

    return {
        "ok": verdict["ok"],
        "current_mode": r.mode.value if r.mode else "",
        "current_label": r.label,
        "target_mode": target.value,
        "target_label": MODE_LABELS[target],
        "market_data_source": MarketDataSource.MAINNET.value
        if MODE_DERIVED[target].get("binance_testnet", s.binance_testnet) is False
        else MarketDataSource.TESTNET.value,
        "requires_restart": True,
        "changes": _mode_changes(r.mode, target),
        "guard": verdict,
        "requires_live_confirmation": needs_live_confirm,
        "risk_summary": _risk_summary() if target is TradingMode.LIVE else [],
        "symbol": s.symbol_list[0] if s.symbol_list else "",
    }


@mode_router.post("/api/trading-mode/apply", dependencies=[Depends(require_admin)])
async def apply_trading_mode(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """保存模式切换。**不重启进程**(§20) —— 由操作者自行重启, 或点管理页的重启按钮。

    `{ "mode": "live", "live_confirmed": true }`
    """
    raw = str(payload.get("mode") or "").strip().lower()
    try:
        target = TradingMode(raw)
    except ValueError:
        return {"ok": False, "error": f"未知模式: {raw!r}; 只接受 paper/testnet/live"}

    live_confirmed = bool(payload.get("live_confirmed"))
    if target is TradingMode.LIVE and not live_confirmed:
        # 实盘确认是一次**人工动作**: 确认弹窗展示风控参数后, 操作者勾选才走到这里。
        return {
            "ok": False,
            "stage": "confirm",
            "error": "进入实盘需要显式确认。请先在确认弹窗中核对真实资金参数并确认。",
            "risk_summary": _risk_summary(),
        }

    cand = _candidate(target, live_confirmed=live_confirmed)
    verdict = _guard_verdict(cand)
    if not verdict["ok"]:
        return {
            "ok": False,
            "stage": "guard",
            "error": "目标模式当前无法启动, 未写入任何配置。",
            "guard": verdict,
        }

    # 写库: TRADING_MODE 为权威。
    #
    # **同时写入推导出的旧字段** —— 否则会踩一个隐蔽的坑: `.env` 里往往还留着迁移前的
    # `PAPER_TRADING=true`(显式设置), 只写 TRADING_MODE 的话, 启动时冲突检查会看到
    # 「TRADING_MODE=testnet 但 PAPER_TRADING=true」而**拒绝启动** —— 那是迁移期旧值,
    # 不是操作者的新意图。把推导值一并落库后两边一致, 冲突检查自然通过。
    changes: dict[str, Any] = {"TRADING_MODE": target.value}
    _attr_to_key = {s.attr: s.key for s in SPECS_BY_KEY.values()}
    for attr, value in MODE_DERIVED[target].items():
        key = _attr_to_key.get(attr)
        if key:
            changes[key] = value
    if target is TradingMode.LIVE:
        changes["LIVE_TRADING_CONFIRM"] = True
        changes["MAINNET_API_SCOPE_CONFIRM"] = True

    from at01_common.runtime_config import save_overrides

    saved = await save_overrides(
        changes,
        by=str(payload.get("by") or "admin")[:64],
        reason=f"模式切换 -> {target.value}",
    )
    if not saved.get("ok"):
        return {"ok": False, "stage": "db", "error": saved.get("error", "写入失败")}

    return {
        "ok": True,
        "stage": "saved",
        "saved": saved["written"],
        "target_mode": target.value,
        "target_label": MODE_LABELS[target],
        "requires_restart": True,
        "restart_hint": _restart_hint(),
        "message": (
            f"已保存「{MODE_LABELS[target]}」配置。需要重启 adaptiveTrading 才能进入该模式;"
            "当前运行模式仍为重启前的模式。"
        ),
    }
