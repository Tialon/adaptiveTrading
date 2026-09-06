"""
WebSocket 推送层(13)

- /ws 端点: 每 2 秒推送行情+分析+风控+环境快照
- broadcast(): 主动广播(如成交事件)
"""

import asyncio
import json
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from at10_web.web_state import system_state

ws_router = APIRouter()

_clients: set[WebSocket] = set()


@ws_router.websocket("/ws")
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
            if system_state.regime_engine:
                payload["regime"] = system_state.regime_engine.snapshot()
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
