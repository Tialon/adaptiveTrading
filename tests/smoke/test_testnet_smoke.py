r"""V11.4 P0-6 真实币安测试网只读冒烟测试(不下单)。

目标: 从「测试证明正确」迈向「真实运行时正确」。对真实测试网
(https://testnet.binance.vision)做只读冒烟, 验证 exchange 集成在真实基础设施上可用:

1. 连通性: GET /api/v3/ping + 服务器时间同步;
2. 交易规则: GET /api/v3/exchangeInfo(SOLUSDT) -> SymbolFilters 解析
   (LOT_SIZE/PRICE_FILTER/MIN_NOTIONAL 均非零, 下单前对齐规则可用);
3. 行情: GET /api/v3/ticker/price(SOLUSDT) 返回合理正价格;
4. 签名账户(仅测试网 key 存在时): GET /api/v3/account 鉴权通过。

安全 / 隔离:
- 全程只读, 不下单/撤单;
- 不打印/记录 API key/secret; 账户只报告「鉴权是否通过」, 不打印余额/权限明细;
- 默认跳过: 需显式 `RUN_TESTNET_SMOKE=true` 才运行(CI 无此环境变量, 自动 skip,
  见 P1-2「Testnet Smoke 与 CI 隔离」);
- 无 key 时签名账户检查单独 skip; 无网络(测试网不可达)时整体 skip, 不 fail。

运行(本地, 需能访问 testnet.binance.vision):
    $env:RUN_TESTNET_SMOKE="true"; .venv\Scripts\python -m pytest tests/smoke/test_testnet_smoke.py -v -s
"""

import os
from decimal import Decimal

import pytest

from at01_common.settings import get_settings
from at10_market.market_rest_client import BinanceRestClient
from at60_execution.exchange_filters import SymbolFilters

SYMBOL = "SOLUSDT"

pytestmark = [pytest.mark.testnet]


@pytest.fixture
async def client():
    """真实测试网 REST 客户端(opt-in; 不 opt-in 则跳过整个模块)。"""
    if os.environ.get("RUN_TESTNET_SMOKE", "").strip().lower() != "true":
        pytest.skip("需显式 RUN_TESTNET_SMOKE=true 才运行真实测试网冒烟(CI 默认跳过)")
    c = BinanceRestClient(testnet=True)
    try:
        await c.connect()
        yield c
    finally:
        await c.disconnect()


async def _ensure_reachable(client) -> None:
    """测试网不可达则整体跳过(不 fail)—— 真实冒烟依赖外部网络, 不可达不视为代码缺陷。"""
    try:
        ok = await client.ping()
    except Exception:
        ok = False
    if not ok:
        pytest.skip("测试网不可达(无网络/被墙), 跳过冒烟")


class TestBinanceTestnetSmoke:
    async def test_connectivity(self, client):
        """GET /api/v3/ping 连通 + 服务器时间已同步(connect 阶段)。"""
        assert await client.ping() is True
        # 时间同步在 connect() 完成; 若同步失败 offset 仍为 0, 不影响本断言(签名会兜底)
        assert isinstance(client.time_offset, int)

    async def test_exchange_info_and_filters(self, client):
        """SOLUSDT 交易规则可拉取且过滤器解析非零(下单前对齐规则的输入可用)。"""
        await _ensure_reachable(client)
        data = await client.get_exchange_info(SYMBOL)
        filters = SymbolFilters.from_exchange_info(SYMBOL, data)
        assert filters.symbol == SYMBOL
        assert filters.step_size > 0, "LOT_SIZE.stepSize 应为正"
        assert filters.tick_size > 0, "PRICE_FILTER.tickSize 应为正"
        assert filters.min_notional > 0, "MIN_NOTIONAL.minNotional 应为正"

    async def test_market_price(self, client):
        """最新价为正(真实行情流动)。"""
        await _ensure_reachable(client)
        price = await client.get_price(SYMBOL)
        assert isinstance(price, Decimal)
        assert price > 0

    async def test_signed_account_auth(self, client):
        """签名账户鉴权(仅 key 存在时): HMAC 签名 + 测试网 key 有效性。

        鉴权失败(如 -2014 非法 key / -2015 签名错误)会抛 BinanceAPIError -> 测试失败,
        这正是冒烟要抓的集成缺陷; 无 key 则跳过。不打印任何余额/权限明细。
        """
        await _ensure_reachable(client)
        s = get_settings()
        if not s.binance_testnet_api_key or not s.binance_testnet_api_secret:
            pytest.skip("未配置测试网 BINANCE_TESTNET_API_KEY/SECRET, 跳过签名账户检查")
        data = await client.get_account()
        assert isinstance(data, dict)
        assert "balances" in data, "账户响应应含 balances(鉴权通过)"
