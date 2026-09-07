"""V11.0(F10/F13): 行情数据一致性(成交流命名空间统一 + myTrades 分页)测试

- F13: WS 成交流从 raw trade(@trade, ID 't')改为 aggTrade(@aggTrade, ID 'a'),
  与 REST 预热/回补 get_agg_trades(取 'a')口径统一, 消除去重/唯一键冲突。
- F10: get_my_trades_all 按 fromId 分页拉全, 不因单页 limit 截断漏成交。
"""

from at20_market.market_rest_client import BinanceRestClient
from at20_market.market_ws_client import BinanceWsClient


class TestStreamsForSymbol:
    def test_subscribes_agg_trade_not_raw(self):
        streams = BinanceWsClient.streams_for_symbol("SOLUSDT")
        assert "solusdt@aggTrade" in streams
        assert "solusdt@trade" not in streams  # V11.0(F13): 不再订阅 raw trade


class TestMyTradesAll:
    async def test_pages_until_short_page(self):
        class _Client(BinanceRestClient):
            def __init__(self):
                self.calls = []

            async def get_my_trades(self, symbol, limit=50, order_id=None,
                                    start_time=None, end_time=None, from_id=None):
                self.calls.append(from_id)
                if from_id is None:
                    return [{"id": 1}, {"id": 2}, {"id": 3}]
                if from_id == 4:
                    return [{"id": 4}, {"id": 5}]
                return []

        c = _Client()
        trades = await c.get_my_trades_all("SOLUSDT", limit=3)
        assert [t["id"] for t in trades] == [1, 2, 3, 4, 5]
        assert c.calls == [None, 4]  # 第二页从上一页末 id+1 继续

    async def test_empty_stops_immediately(self):
        class _Client(BinanceRestClient):
            def __init__(self):
                self.calls = []

            async def get_my_trades(self, symbol, limit=50, order_id=None,
                                    start_time=None, end_time=None, from_id=None):
                self.calls.append(from_id)
                return []

        c = _Client()
        trades = await c.get_my_trades_all("SOLUSDT", limit=3)
        assert trades == []
        assert c.calls == [None]
