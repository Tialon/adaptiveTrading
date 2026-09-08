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
from at10_web.web_auth import require_admin
from at10_web.web_state import system_state

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
    """V11.2 P1-2: 生产可观测性指标快照 + 阈值告警 + 策略归因。"""
    from at50_execution.observability import evaluate_alerts, strategy_attribution

    store = system_state.metrics
    if store is None:
        return {"snapshot": {}, "alerts": [], "attribution": []}
    return {
        "snapshot": store.snapshot(),
        "alerts": [a.to_dict() for a in evaluate_alerts(store)],
        "attribution": strategy_attribution(store),
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
    rm.kill_switch.arm("人工急停")
    await rm.kill_switch.persist()
    await rm._record_event("kill_switch", "人工急停")
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
    return {"ok": True, "armed": rm.kill_switch.is_armed}


@router.post("/api/shutdown", dependencies=[Depends(require_admin)])
async def shutdown() -> dict[str, Any]:
    """请求主程序优雅停机(设置停止标志)"""
    system_state.extra["shutdown_requested"] = True
    return {"ok": True}
