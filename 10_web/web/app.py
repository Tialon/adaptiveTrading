"""
Web 应用(FastAPI)

REST API:
- GET /                     系统总览
- GET /api/market           行情快照
- GET /api/analytics        分析快照
- GET /api/risk             风控状态
- GET /api/positions        持仓
- GET /api/orders           近期订单(DB)
- GET /api/signals          近期信号(DB)
- GET /api/whales           近期大单
- POST /api/breaker/reset   手动解除熔断
- WS  /ws                   实时推送(行情+分析)
"""

import asyncio
import json
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from common.config.settings import get_settings
from common.models import Order, Signal
from web.state import system_state

app = FastAPI(title="adaptiveTrading", version=get_settings().app_version)

_STATIC_DIR = Path(__file__).parent / "static"


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    """监控面板首页"""
    html = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "running": system_state.running}


@app.get("/api/system")
async def system_summary() -> dict[str, Any]:
    return system_state.summary()


@app.get("/api/market")
async def market(symbol: Optional[str] = None) -> dict[str, Any]:
    me = system_state.market_engine
    if me is None:
        return {"symbols": {}}
    return {"symbols": me.snapshot(symbol)}


@app.get("/api/analytics")
async def analytics() -> dict[str, Any]:
    ae = system_state.analytics_engine
    if ae is None:
        return {"symbols": {}, "whales": []}
    return ae.snapshot()


@app.get("/api/risk")
async def risk() -> dict[str, Any]:
    rm = system_state.risk_manager
    if rm is None:
        return {}
    return rm.status()


@app.get("/api/positions")
async def positions() -> dict[str, Any]:
    rm = system_state.risk_manager
    if rm is None:
        return {"positions": []}
    return {
        "positions": [
            {**p.to_dict(), "unrealized_pnl": round(rm.positions.unrealized_pnl(s, (system_state.market_engine.state[s].last_price if system_state.market_engine and s in system_state.market_engine.state else p.avg_price)), 2)}
            for s, p in rm.positions.positions.items()
            if p.quantity > 0
        ]
    }


@app.get("/api/orders")
async def orders(limit: int = 50) -> dict[str, Any]:
    from common.config.database import AsyncSessionLocal

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


@app.get("/api/signals")
async def signals(limit: int = 50) -> dict[str, Any]:
    from common.config.database import AsyncSessionLocal

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
                    "status": r.status,
                    "created_at": str(r.created_at),
                }
                for r in rows
            ]
        }


@app.post("/api/breaker/reset")
async def breaker_reset() -> dict[str, Any]:
    rm = system_state.risk_manager
    if rm is None:
        return {"ok": False, "msg": "not running"}
    rm.breaker.reset()
    return {"ok": True}


@app.post("/api/shutdown")
async def shutdown() -> dict[str, Any]:
    """请求主程序优雅停机(设置停止标志)"""
    system_state.extra["shutdown_requested"] = True
    return {"ok": True}


# ---------- WebSocket 推送 ----------

_clients: set[WebSocket] = set()
_push_task: Optional[asyncio.Task] = None


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    """实时推送:每 2 秒推送行情+分析快照"""
    await ws.accept()
    _clients.add(ws)
    try:
        while True:
            payload: dict[str, Any] = {}
            if system_state.market_engine:
                payload["market"] = {
                    "symbols": {
                        s: {
                            "last_price": st.last_price,
                            "best_bid": st.depth.best_bid,
                            "best_ask": st.depth.best_ask,
                            "change_pct_24h": st.mark_change_pct_24h,
                        }
                        for s, st in system_state.market_engine.state.items()
                    }
                }
            if system_state.analytics_engine:
                latest = system_state.analytics_engine.get_all()
                payload["analytics"] = {s: a.to_dict() for s, a in latest.items()}
            if system_state.risk_manager:
                payload["risk"] = {
                    "drawdown": system_state.risk_manager.drawdown.status(),
                    "breaker": system_state.risk_manager.breaker.status(),
                }
            if system_state.execution_engine:
                payload["execution"] = system_state.execution_engine.status()
            await ws.send_text(json.dumps(payload, ensure_ascii=False))
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        pass
    finally:
        _clients.discard(ws)


async def broadcast(message: dict[str, Any]) -> None:
    """向所有 WS 客户端广播(如成交事件)"""
    text = json.dumps(message, ensure_ascii=False)
    for ws in list(_clients):
        try:
            await ws.send_text(text)
        except Exception:
            _clients.discard(ws)


async def start_server(host: str = "0.0.0.0", port: int = 8800) -> None:
    """启动 API 服务"""
    import uvicorn

    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()
