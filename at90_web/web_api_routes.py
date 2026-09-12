"""
REST API 层(12)

全部路由注册到 APIRouter,由 app.py 挂载。
"""

from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from at01_common.models import Order, Signal
from at01_common.operator_events import KIND_KILL, KIND_RECOVER, operator_log
from at01_common.operator_narrative import KILL_ORIGIN_MANUAL
from at90_web.web_auth import require_admin
from at90_web.web_state import system_state

router = APIRouter()

_STATIC_DIR = Path(__file__).parent / "static"


@router.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    """监控面板首页"""
    html = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@router.get("/api/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "running": system_state.running}


@router.get("/api/system")
async def system_summary() -> dict[str, Any]:
    return system_state.summary()


@router.get("/api/metrics")
async def metrics() -> dict[str, Any]:
    """V11.2 P1-2: 生产可观测性指标快照 + 阈值告警 + 策略归因。
    V11.5 P1-1: 追加统一运行时健康快照(runtime health + 单一状态分类)。
    """
    from at01_common.runtime_health import build_runtime_health
    from at60_execution.observability import evaluate_alerts, strategy_attribution

    store = system_state.metrics
    if store is None:
        return {
            "snapshot": {},
            "alerts": [],
            "attribution": [],
            "health": build_runtime_health(system_state),
        }
    return {
        "snapshot": store.snapshot(),
        "alerts": [a.to_dict() for a in evaluate_alerts(store)],
        "attribution": strategy_attribution(store),
        "health": build_runtime_health(system_state),
    }


@router.get("/ops", response_class=HTMLResponse)
async def ops_page() -> HTMLResponse:
    """V12.2: 只读部署检查页(Pi 上线前快速自检; 无任何危险按钮)。"""
    html = (_STATIC_DIR / "ops.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@router.get("/api/operator-status")
async def operator_status() -> dict[str, Any]:
    """V12.2: 操作者状态聚合(只读)。

    交易许可单一权威: `can_buy`/`can_sell` 直接取自 `build_runtime_health`
    (其本身取自 `TradingGate`), 本接口不重新实现交易判定。
    系统未完全启动时也返回可读 JSON, 不抛 500。
    """
    from at01_common.runtime_health import build_runtime_health
    from at01_common.settings import get_settings
    from at90_web.web_operator_status import build_operator_status

    try:
        health = build_runtime_health(system_state)
    except Exception:  # 引擎半初始化时快照可能失败, 降级为「闸门未就绪」而非 500
        health = {}
    return build_operator_status(get_settings(), health)


@router.get("/api/operator-log")
async def operator_log_endpoint(limit: int = 100, kind: str = "", source: str = "auto") -> dict[str, Any]:
    """V13 P0: 操作员事件流(只读, 无需令牌) —— 「今天发生了什么」。

    `source=ring`(默认 auto)优先读内存环(同步、最新); 内存环为空(如刚重启)时回落到
    读库, 让用户在重启后**仍能看到今天早些时候发生了什么**。`source=db` 强制读库。

    只读接口: 本端点没有任何写入路径, 也不暴露密钥(事件在 `emit()` 时已脱敏)。
    """
    from at01_common.operator_events import operator_log

    if source == "db":
        events = await operator_log.load_recent(limit)
    else:
        events = operator_log.recent(limit, kind=kind)
        if not events:
            events = await operator_log.load_recent(limit)
    if kind and source == "db":
        events = [e for e in events if e.get("kind") == kind]
    return {
        "events": events,
        "count": len(events),
        "kind": kind,
        "source": source,
        "self": operator_log.status(),
    }


@router.get("/api/market")
async def market(symbol: Optional[str] = None) -> dict[str, Any]:
    me = system_state.market_engine
    if me is None:
        return {"symbols": {}}
    return {"symbols": me.snapshot(symbol)}


@router.get("/api/analytics")
async def analytics() -> dict[str, Any]:
    ae = system_state.analytics_engine
    if ae is None:
        return {"symbols": {}, "whales": []}
    return ae.snapshot()


@router.get("/api/regime")
async def regime() -> dict[str, Any]:
    """V2.0: 市场环境"""
    re_ = system_state.regime_engine
    if re_ is None:
        return {"regimes": {}}
    return {"regimes": re_.snapshot()}


@router.get("/api/risk")
async def risk() -> dict[str, Any]:
    rm = system_state.risk_manager
    if rm is None:
        return {}
    return rm.status()


@router.get("/api/positions")
async def positions() -> dict[str, Any]:
    rm = system_state.risk_manager
    if rm is None:
        return {"positions": []}
    me = system_state.market_engine
    return {
        "positions": [
            {
                **p.to_dict(),
                "unrealized_pnl": round(
                    rm.positions.unrealized_pnl(
                        s,
                        (me.state[s].last_price if me and s in me.state else p.avg_price),
                    ),
                    2,
                ),
            }
            for s, p in rm.positions.positions.items()
            if p.quantity > 0
        ]
    }


@router.get("/api/orders")
async def orders(limit: int = 50) -> dict[str, Any]:
    from at01_common.database import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(Order).order_by(Order.id.desc()).limit(min(limit, 200))
                )
            )
            .scalars()
            .all()
        )
        return {
            "orders": [
                {
                    "id": r.id,
                    "client_order_id": r.client_order_id,
                    "symbol": r.symbol,
                    "side": r.side,
                    "type": r.order_type,
                    "price": r.price,
                    "quantity": r.quantity,
                    "filled": r.filled_quantity,
                    "status": r.status,
                    "strategy": r.strategy,
                    "is_paper": r.is_paper,
                    "created_at": str(r.created_at),
                }
                for r in rows
            ]
        }


@router.get("/api/signals")
async def signals(limit: int = 50) -> dict[str, Any]:
    from at01_common.database import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(Signal).order_by(Signal.id.desc()).limit(min(limit, 200))
                )
            )
            .scalars()
            .all()
        )
        return {
            "signals": [
                {
                    "id": r.id,
                    "symbol": r.symbol,
                    "strategy": r.strategy,
                    "side": r.side,
                    "price": r.price,
                    "quantity": r.quantity,
                    "reason": r.reason,
                    "score": r.score,
                    "indicators": r.indicators,
                    "status": r.status,
                    "created_at": str(r.created_at),
                }
                for r in rows
            ]
        }


@router.get("/api/equity-curve")
async def equity_curve(symbol: str = "SOLUSDT", limit: int = 200) -> dict[str, Any]:
    """V2.0: 持仓快照曲线(position_snapshot 表)"""
    from at01_common.database import AsyncSessionLocal
    from at01_common.models import PositionSnapshot

    async with AsyncSessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(PositionSnapshot)
                    .where(PositionSnapshot.symbol == symbol)
                    .order_by(PositionSnapshot.id.desc())
                    .limit(min(limit, 1000))
                )
            )
            .scalars()
            .all()
        )
        rows.reverse()
        return {
            "symbol": symbol,
            "points": [
                {
                    "ts": str(r.timestamp),
                    "equity": r.equity,
                    "unrealized": r.unrealized_profit,
                    "realized": r.realized_profit,
                    "market_price": r.market_price,
                }
                for r in rows
            ],
        }


@router.get("/api/strategy-performance")
async def strategy_performance() -> dict[str, Any]:
    """V2.0: 策略绩效"""
    from at01_common.database import AsyncSessionLocal
    from at01_common.models import StrategyPerformance

    async with AsyncSessionLocal() as session:
        rows = (await session.execute(select(StrategyPerformance))).scalars().all()
        return {
            "performance": [
                {
                    "strategy": r.strategy,
                    "symbol": r.symbol,
                    "trade_count": r.trade_count,
                    "win_rate": round(r.win_rate, 3),
                    "profit": round(r.profit, 2),
                }
                for r in rows
            ]
        }


@router.post("/api/breaker/reset", dependencies=[Depends(require_admin)])
async def breaker_reset() -> dict[str, Any]:
    rm = system_state.risk_manager
    if rm is None:
        return {"ok": False, "msg": "not running"}
    rm.breaker.reset()
    return {"ok": True}


@router.post("/api/emergency/kill", dependencies=[Depends(require_admin)])
async def emergency_kill() -> dict[str, Any]:
    """V10: 人工急停 — 冻结交易 + 撤销全部未成交订单(持久化, 需 recover 解除)"""
    from at01_common.settings import get_settings

    rm = system_state.risk_manager
    ex = system_state.execution_engine
    if rm is None:
        return {"ok": False, "msg": "not running"}
    settings = get_settings()
    # V13: 显式标 MANUAL —— 人工急停**永远**不会自动解除, 必须人工 recover。
    rm.kill_switch.arm("人工急停", origin=KILL_ORIGIN_MANUAL)
    await rm.kill_switch.persist()
    await rm._record_event("kill_switch", "人工急停")
    operator_log.emit(
        KIND_KILL, "人工急停: 交易已停止", level="KILLED",
        detail={"actor": "human", "origin": KILL_ORIGIN_MANUAL, "reason": "人工急停"},
    )
    canceled = 0
    if ex is not None:
        # V11.3 P0-4: 撤单也走统一交易闸门(单一权威); 急停撤单为风险收敛动作,
        # can_cancel_order 仅 STOPPED 时拒绝(正常急停场景放行)。
        gate = system_state.trading_gate
        cancel_ok, cancel_reason = (True, "")
        if gate is not None:
            cancel_ok, cancel_reason = gate.can_cancel_order()
        if not cancel_ok:
            return {
                "ok": True,
                "armed": rm.kill_switch.is_armed,
                "canceled": 0,
                "cancel_blocked": cancel_reason,
            }
        for symbol in settings.symbol_list:
            canceled += await ex.cancel_all_open_orders(symbol)
    return {"ok": True, "armed": rm.kill_switch.is_armed, "canceled": canceled}


@router.post("/api/emergency/recover", dependencies=[Depends(require_admin)])
async def emergency_recover() -> dict[str, Any]:
    """V10: 解除急停(人工恢复交易)"""
    rm = system_state.risk_manager
    if rm is None:
        return {"ok": False, "msg": "not running"}
    rm.kill_switch.disarm()
    await rm.kill_switch.persist()
    await rm._record_event("kill_switch", "人工恢复")
    operator_log.emit(
        KIND_RECOVER, "人工恢复: 已解除急停", level="NOTICE",
        detail={"actor": "human", "reason": "人工恢复"},
    )
    return {"ok": True, "armed": rm.kill_switch.is_armed}


@router.post("/api/shutdown", dependencies=[Depends(require_admin)])
async def shutdown() -> dict[str, Any]:
    """请求主程序优雅停机。

    **V12.3 修复**: 此前只置 `system_state.extra["shutdown_requested"] = True`, 而全代码库
    **没有任何地方读该标志** —— 这个端点一直是空操作。现改为投递真实的停机请求
    (`runtime.request_shutdown()`), 与 Ctrl+C / `docker stop` 走同一条优雅停机路径。

    注意: 容器内 `restart: unless-stopped` 会把退出后的容器**重新拉起**, 所以容器里
    「停机」的实际效果等同于重启; 要真正停下请用 `docker compose stop`。
    """
    from at01_common.runtime import in_container, request_shutdown

    system_state.extra["shutdown_requested"] = True
    delivered = request_shutdown()
    return {
        "ok": delivered,
        "containerized": in_container(),
        "msg": "" if delivered else "无法投递停机请求(当前进程未运行在主循环中)",
        "note": (
            "容器 restart 策略为 unless-stopped, 退出后会被自动拉起 —— 容器内该操作实际等同重启。"
            if in_container() else "非容器运行, 进程退出后需手动重新启动。"
        ),
    }
