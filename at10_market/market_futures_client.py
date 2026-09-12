"""
币安 USDT-M 合约 REST 客户端(V9.0 M3.4) — 情绪因子数据源

仅取公共数据(Funding Rate / Open Interest), 无需签名。默认关闭, 不碰现货主链路。
HTTP 调用抽成 `_get(path)` 便于测试 monkeypatch。
"""

from typing import Any, Optional

import aiohttp

from at01_common.logger import LoggerMixin
from at01_common.settings import get_settings


class BinanceFuturesClient(LoggerMixin):
    """币安 USDT-M 合约公共行情客户端"""

    def __init__(self, base_url: Optional[str] = None):
        self.settings = get_settings()
        self.base_url = base_url or self.settings.binance_futures_base_url
        self.session: Optional[aiohttp.ClientSession] = None

    async def connect(self) -> None:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
            )

    async def disconnect(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    async def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> Any:
        """HTTP GET(测试可 monkeypatch 此方法)"""
        await self.connect()
        url = f"{self.base_url}{path}"
        async with self.session.get(url, params=params or {}) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                raise RuntimeError(f"合约 API 错误 [{resp.status}]: {data}")
            return data

    async def get_funding_rate(self, symbol: str) -> float:
        """最新资金费率(8 小时费率, 正=多头付空头)"""
        data = await self._get("/fapi/v1/fundingRate", {"symbol": symbol, "limit": 1})
        if isinstance(data, list) and data:
            return float(data[-1].get("fundingRate", 0.0))
        return 0.0

    async def get_open_interest(self, symbol: str) -> float:
        """当前未平仓合约数量(张)"""
        data = await self._get("/fapi/v1/openInterest", {"symbol": symbol})
        return float(data.get("openInterest", 0.0))
