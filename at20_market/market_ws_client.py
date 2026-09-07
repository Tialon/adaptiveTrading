"""
币安 WebSocket 客户端(组合流、自动重连)
"""

import asyncio
import json
from typing import Any, Awaitable, Callable, Optional

import aiohttp

from at01_common.settings import get_settings
from at01_common.logger import LoggerMixin

MessageHandler = Callable[[dict[str, Any]], Awaitable[None]]


class BinanceWsClient(LoggerMixin):
    """币安 WebSocket 客户端

    使用组合流 (/stream?streams=...) 订阅多个标的的多类数据:
    trade / kline / depth / ticker
    """

    def __init__(
        self,
        on_message: Optional[MessageHandler] = None,
        ws_url: Optional[str] = None,
        reconnect_interval: float = 5.0,
        on_reconnect: Optional[Callable[[], Awaitable[None]]] = None,
    ):
        settings = get_settings()
        self.ws_url = ws_url or settings.binance_stream_url
        self.on_message = on_message
        self.reconnect_interval = reconnect_interval
        # V10.5: WS 断线重连回调(重连前触发, 供上层 REST 回补缺口数据)
        self.on_reconnect = on_reconnect

        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._streams: set[str] = set()
        self._running = False
        self._task: Optional[asyncio.Task] = None

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    def _build_url(self) -> str:
        """组合流 URL"""
        streams = "/".join(sorted(self._streams))
        return f"{self.ws_url.replace('/ws', '/stream')}?streams={streams}"

    async def add_streams(self, streams: list[str]) -> None:
        """添加订阅(重连后生效,连接中则动态订阅)"""
        self._streams.update(streams)
        if self.connected:
            await self._ws.send_json({"method": "SUBSCRIBE", "params": streams, "id": 1})
        self.logger.info("订阅流", streams=streams)

    async def remove_streams(self, streams: list[str]) -> None:
        """取消订阅"""
        self._streams -= set(streams)
        if self.connected:
            await self._ws.send_json({"method": "UNSUBSCRIBE", "params": streams, "id": 2})

    @staticmethod
    def streams_for_symbol(symbol: str, kline_interval: str = "1m", depth_level: int = 20) -> list[str]:
        """一个标的标准订阅集合"""
        s = symbol.lower()
        return [
            f"{s}@trade",
            f"{s}@kline_{kline_interval}",
            f"{s}@depth{depth_level}@100ms",
            f"{s}@ticker",
        ]

    async def _connect_once(self) -> None:
        """建立单次连接并消费"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        url = self._build_url()
        self._ws = await self._session.ws_connect(url, heartbeat=30)
        self.logger.info("WebSocket 已连接", url=url)

        async for msg in self._ws:
            if not self._running:
                break
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                if self.on_message:
                    try:
                        await self.on_message(data)
                    except Exception:
                        self.logger.exception("消息处理异常")
            elif msg.type == aiohttp.WSMsgType.ERROR:
                self.logger.error("WebSocket 错误", error=str(self._ws.exception()))
                break
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE):
                self.logger.warning("WebSocket 被关闭")
                break

    async def _run_loop(self) -> None:
        """带自动重连的运行循环"""
        while self._running:
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger.error("WebSocket 连接异常", error=str(e))
            if self._running:
                # V10.5: 断线后、重连前回补缺口(行情引擎 REST 重拉)
                if self.on_reconnect:
                    try:
                        await self.on_reconnect()
                    except Exception:
                        self.logger.exception("重连回调异常")
                self.logger.info("重连", seconds=self.reconnect_interval)
                await asyncio.sleep(self.reconnect_interval)

    async def start(self) -> None:
        """启动后台监听任务"""
        if self._task is not None and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name="binance-ws")

    async def stop(self) -> None:
        """停止并清理"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()
        self.logger.info("WebSocket 已停止")
