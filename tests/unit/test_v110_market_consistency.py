"""V11.0(F10/F13): 行情数据一致性(成交流命名空间统一 + myTrades 分页)测试

- F13: WS 成交流从 raw trade(@trade, ID 't')改为 aggTrade(@aggTrade, ID 'a'),
  与 REST 预热/回补 get_agg_trades(取 'a')口径统一, 消除去重/唯一键冲突。
- F10: get_my_trades_all 按 fromId 分页拉全, 不因单页 limit 截断漏成交。
"""

from at10_market.market_rest_client import BinanceRestClient
from at10_market.market_ws_client import BinanceWsClient


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
        result = await c.get_my_trades_all("SOLUSDT", limit=3)
        assert [t["id"] for t in result.trades] == [1, 2, 3, 4, 5]
        assert result.complete is True
        assert result.pagination_exhausted is False
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
        result = await c.get_my_trades_all("SOLUSDT", limit=3)
        assert result.trades == []
        assert result.complete is True
        assert c.calls == [None]

    async def test_pagination_exhausted_flags_incomplete(self):
        # 每页都返回满页, 翻满 max_pages -> 截断, 标记不完整且保留已拉取成交
        class _Client(BinanceRestClient):
            def __init__(self):
                self.calls = 0

            async def get_my_trades(self, symbol, limit=50, order_id=None,
                                    start_time=None, end_time=None, from_id=None):
                self.calls += 1
                base = (from_id or 0) + 1
                return [{"id": base + i} for i in range(limit)]

        c = _Client()
        result = await c.get_my_trades_all("SOLUSDT", limit=3, max_pages=2)
        assert result.pagination_exhausted is True
        assert result.complete is False
        assert c.calls == 2
        assert len(result.trades) == 6  # 2 页 × 3 条, 已拉取部分仍保留

    async def test_detects_duplicate_ids_and_dedups(self):
        # 两页重叠: 第二页重复返回 id=3 -> 去重 + 检测 duplicate_ids
        class _Client(BinanceRestClient):
            def __init__(self):
                self.calls = []

            async def get_my_trades(self, symbol, limit=50, order_id=None,
                                    start_time=None, end_time=None, from_id=None):
                self.calls.append(from_id)
                if from_id is None:
                    return [{"id": 1}, {"id": 2}, {"id": 3}]
                if from_id == 4:
                    return [{"id": 3}, {"id": 4}]  # 重叠 id=3
                return []

        c = _Client()
        result = await c.get_my_trades_all("SOLUSDT", limit=3)
        assert [t["id"] for t in result.trades] == [1, 2, 3, 4]  # 去重后升序
        assert result.duplicate_ids == [3]

    async def test_detects_gap(self):
        # id 跳号(1 -> 5) -> gaps 记录缺失区间; 完整(单页短页)仍 complete
        class _Client(BinanceRestClient):
            async def get_my_trades(self, symbol, limit=50, order_id=None,
                                    start_time=None, end_time=None, from_id=None):
                return [{"id": 1}, {"id": 5}]

        c = _Client()
        result = await c.get_my_trades_all("SOLUSDT", limit=3)
        assert result.complete is True
        assert result.gaps == [{"from": 1, "to": 5, "missing": 3}]
