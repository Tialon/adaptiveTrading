"""
币安 REST API 客户端
"""

import hashlib
import hmac
import time
from decimal import Decimal
from typing import Any, Optional
from urllib.parse import urlencode

import aiohttp

from at01_common.settings import get_settings
from at01_common.logger import LoggerMixin


class BinanceAPIError(Exception):
    """币安 API 异常"""

    def __init__(self, status: int, code: Any, msg: str):
        self.status = status
        self.code = code
        self.msg = msg
        super().__init__(f"API 错误 [{status}] code={code}: {msg}")


class BinanceRestClient(LoggerMixin):
    """币安 REST 客户端(自动同步服务器时间、HMAC 签名)"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        base_url: Optional[str] = None,
        testnet: Optional[bool] = None,
    ):
        settings = get_settings()
        self.testnet = testnet if testnet is not None else settings.binance_testnet

        if self.testnet:
            self.api_key = api_key or settings.binance_testnet_api_key
            self.api_secret = api_secret or settings.binance_testnet_api_secret
            self.base_url = base_url or settings.binance_testnet_base_url
        else:
            self.api_key = api_key or settings.binance_api_key
            self.api_secret = api_secret or settings.binance_api_secret
            self.base_url = base_url or settings.binance_base_url

        self.session: Optional[aiohttp.ClientSession] = None
        self.time_offset: int = 0

    async def connect(self) -> None:
        """建立 HTTP 会话并同步服务器时间"""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                headers={"X-MBX-APIKEY": self.api_key},
                timeout=aiohttp.ClientTimeout(total=15),
            )
            await self._sync_server_time()
            self.logger.info("REST 会话建立", base_url=self.base_url)

    async def disconnect(self) -> None:
        """关闭会话"""
        if self.session and not self.session.closed:
            await self.session.close()

    async def _sync_server_time(self) -> None:
        """同步服务器时间偏移"""
        try:
            data = await self._request("GET", "/api/v3/time")
            self.time_offset = data["serverTime"] - int(time.time() * 1000)
            self.logger.info("服务器时间已同步", offset_ms=self.time_offset)
        except Exception as e:
            self.logger.warning("时间同步失败", error=str(e))

    def _sign(self, params: dict[str, Any]) -> str:
        """HMAC-SHA256 签名"""
        query = urlencode(params)
        return hmac.new(
            self.api_secret.encode(),
            query.encode(),
            hashlib.sha256,
        ).hexdigest()

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict[str, Any]] = None,
        signed: bool = False,
    ) -> Any:
        """发送请求"""
        if self.session is None or self.session.closed:
            await self.connect()

        params = dict(params or {})
        if signed:
            params["timestamp"] = int(time.time() * 1000) + self.time_offset
            params["recvWindow"] = 5000
            params["signature"] = self._sign(params)

        url = f"{self.base_url}{path}"
        async with self.session.request(method, url, params=params) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                if isinstance(data, dict):
                    raise BinanceAPIError(resp.status, data.get("code"), data.get("msg", ""))
                raise BinanceAPIError(resp.status, None, str(data))
            return data

    # ---------- 公共行情 ----------

    async def ping(self) -> bool:
        """连通性测试"""
        try:
            await self._request("GET", "/api/v3/ping")
            return True
        except Exception:
            return False

    async def get_price(self, symbol: str) -> Decimal:
        """最新价格"""
        data = await self._request("GET", "/api/v3/ticker/price", {"symbol": symbol})
        return Decimal(data["price"])

    async def get_ticker(self, symbol: str) -> dict[str, Any]:
        """24h 行情"""
        return await self._request("GET", "/api/v3/ticker/24hr", {"symbol": symbol})

    async def get_klines(
        self,
        symbol: str,
        interval: str = "1m",
        limit: int = 200,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
    ) -> list[list[Any]]:
        """K线"""
        params: dict[str, Any] = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time
        return await self._request("GET", "/api/v3/klines", params)

    async def get_depth(self, symbol: str, limit: int = 20) -> dict[str, Any]:
        """订单簿"""
        return await self._request("GET", "/api/v3/depth", {"symbol": symbol, "limit": limit})

    async def get_exchange_info(self, symbol: Optional[str] = None) -> dict[str, Any]:
        """交易规则(LOT_SIZE/PRICE_FILTER/MIN_NOTIONAL 等过滤器), 供下单前对齐"""
        params = {"symbol": symbol} if symbol else {}
        return await self._request("GET", "/api/v3/exchangeInfo", params)

    async def get_agg_trades(self, symbol: str, limit: int = 500) -> list[dict[str, Any]]:
        """聚合成交"""
        return await self._request("GET", "/api/v3/aggTrades", {"symbol": symbol, "limit": limit})

    # ---------- 账户与订单(需签名) ----------

    async def get_account(self) -> dict[str, Any]:
        """账户信息"""
        return await self._request("GET", "/api/v3/account", signed=True)

    async def create_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Decimal,
        price: Optional[Decimal] = None,
        time_in_force: str = "GTC",
        new_client_order_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """下单"""
        params: dict[str, Any] = {
            "symbol": symbol,
            "side": side.upper(),
            "type": order_type.upper(),
            "quantity": f"{quantity:f}",
        }
        if new_client_order_id:
            params["newClientOrderId"] = new_client_order_id
        if order_type.upper() == "LIMIT":
            if price is None:
                raise ValueError("限价单必须指定价格")
            params["price"] = f"{price:f}"
            params["timeInForce"] = time_in_force
        return await self._request("POST", "/api/v3/order", params, signed=True)

    async def cancel_order(self, symbol: str, order_id: str) -> dict[str, Any]:
        """撤单"""
        return await self._request(
            "DELETE", "/api/v3/order", {"symbol": symbol, "orderId": order_id}, signed=True
        )

    async def get_order(
        self,
        symbol: str,
        order_id: Optional[str] = None,
        orig_client_order_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """查询订单(按交易所订单 ID 或原始 clientOrderId, 二选一)"""
        params: dict[str, Any] = {"symbol": symbol}
        if orig_client_order_id:
            params["origClientOrderId"] = orig_client_order_id
        elif order_id is not None:
            params["orderId"] = order_id
        return await self._request("GET", "/api/v3/order", params, signed=True)

    async def get_open_orders(self, symbol: Optional[str] = None) -> list[dict[str, Any]]:
        """未成交订单"""
        params = {"symbol": symbol} if symbol else {}
        return await self._request("GET", "/api/v3/openOrders", params, signed=True)

    async def get_my_trades(
        self,
        symbol: str,
        limit: int = 50,
        order_id: Optional[str] = None,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        from_id: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """成交历史(需签名, V10 启动对账崩溃窗口恢复用); order_id 可过滤单订单

        V11.0(F10): 新增 start_time/end_time(ms)/from_id 分页参数。注意 order_id 与
        start_time/end_time/from_id 互斥(币安限制), 调用方二选一。
        """
        params: dict[str, Any] = {"symbol": symbol, "limit": limit}
        if order_id:
            params["orderId"] = order_id
        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time
        if from_id is not None:
            params["fromId"] = from_id
        return await self._request("GET", "/api/v3/myTrades", params, signed=True)

    async def get_my_trades_all(
        self,
        symbol: str,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        limit: int = 1000,
        max_pages: int = 10,
    ) -> list[dict[str, Any]]:
        """分页拉全成交历史(fromId 翻页, 最多 max_pages 页)。

        V11.0(F10): 对账/恢复兜底不因单页 limit 截断漏掉近期成交; 每页从上一页最后
        一条成交 id + 1 继续, 单页不足 limit 或翻页次数耗尽即停(幂等、有界)。
        """
        out: list[dict[str, Any]] = []
        from_id: Optional[int] = None
        for _ in range(max_pages):
            batch = await self.get_my_trades(
                symbol, limit=limit, start_time=start_time,
                end_time=end_time, from_id=from_id,
            )
            if not batch:
                break
            out.extend(batch)
            if len(batch) < limit:
                break
            from_id = int(batch[-1].get("id", 0)) + 1
        return out
