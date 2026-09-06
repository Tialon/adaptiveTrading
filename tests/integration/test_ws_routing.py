"""
行情引擎 WS 消息路由集成测试

用真实币安组合流的消息格式(aggTrade / raw trade / depth / kline / ticker)
驱动 MarketDataEngine 的 _on_ws_message,验证状态正确更新。
不连接网络,纯内存。
"""

import pytest

from market.engine import MarketDataEngine


@pytest.fixture
def engine():
    """内存行情引擎(mock 网络启动)"""
    eng = MarketDataEngine(symbols=["BTCUSDT"])
    return eng


def _ws(stream: str, data: dict) -> dict:
    """构造组合流外层消息"""
    return {"stream": stream, "data": data}


AGG_TRADE = {
    "e": "aggTrade", "E": 1788690000000, "s": "BTCUSDT",
    "a": 123456789, "p": "79979.50", "q": "0.00125",
    "f": 100, "l": 101, "T": 1788690000000, "m": False,
}

RAW_TRADE = {
    "e": "trade", "E": 1788690000000, "s": "BTCUSDT",
    "t": 999, "p": "79980.10", "q": "0.002", "T": 1788690000000, "m": True,
}

DEPTH = {
    "e": "depthUpdate", "E": 1788690000000, "s": "BTCUSDT",
    "U": 1, "u": 2,
    "b": [["79979.00", "1.500"], ["79978.00", "0.750"]],
    "a": [["79980.00", "2.000"], ["79981.00", "0.500"]],
}

KLINE = {
    "e": "kline", "E": 1788690000000, "s": "BTCUSDT",
    "k": {
        "t": 1788690000000, "T": 1788690059999, "s": "BTCUSDT", "i": "1m",
        "f": 100, "L": 200, "o": "79979.00", "c": "79981.00",
        "h": "79982.00", "l": "79978.00", "v": "12.5",
        "n": 100, "x": False, "q": "1000000", "V": "6.2", "Q": "500000",
    },
}

TICKER = {
    "e": "24hrTicker", "E": 1788690000000, "s": "BTCUSDT",
    "p": "100.00", "P": "0.41", "c": "79979.23",
    "h": "80424.74", "l": "79113.39", "q": "52242942.67",
}


class TestWsRouting:
    async def test_agg_trade_updates_state(self, engine):
        await engine._on_ws_message(_ws("btcusdt@trade", AGG_TRADE))
        st = engine.state["BTCUSDT"]
        assert st.last_price == pytest.approx(79979.50)
        assert len(st.trades) == 1
        tick = st.trades[0]
        assert tick.trade_id == 123456789
        assert tick.quantity == pytest.approx(0.00125)
        assert tick.quote_quantity == pytest.approx(79979.50 * 0.00125)
        assert tick.is_buyer_maker is False

    async def test_raw_trade_updates_state(self, engine):
        await engine._on_ws_message(_ws("btcusdt@trade", RAW_TRADE))
        st = engine.state["BTCUSDT"]
        assert st.last_price == pytest.approx(79980.10)
        assert st.trades[0].trade_id == 999
        assert st.trades[0].is_buyer_maker is True

    async def test_depth_updates_book(self, engine):
        await engine._on_ws_message(_ws("btcusdt@depth20@100ms", DEPTH))
        d = engine.state["BTCUSDT"].depth
        assert d.best_bid == pytest.approx(79979.00)
        assert d.best_ask == pytest.approx(79980.00)
        assert d.spread == pytest.approx(1.00)
        assert d.mid_price == pytest.approx(79979.50)

    async def test_kline_updates_bars(self, engine):
        await engine._on_ws_message(_ws("btcusdt@kline_1m", KLINE))
        st = engine.state["BTCUSDT"]
        assert len(st.klines) == 1
        bar = st.klines[0]
        assert bar.open_time == 1788690000000
        assert bar.close == pytest.approx(79981.00)
        assert bar.closed is False
        assert st.last_price == pytest.approx(79981.00)

        # 同一根K线的更新应覆盖,不新增
        k2 = {**KLINE, "k": {**KLINE["k"], "c": "79985.00", "x": True}}
        await engine._on_ws_message(_ws("btcusdt@kline_1m", k2))
        assert len(st.klines) == 1
        assert st.klines[0].close == pytest.approx(79985.00)
        assert st.klines[0].closed is True

    async def test_ticker_updates_24h_stats(self, engine):
        await engine._on_ws_message(_ws("btcusdt@ticker", TICKER))
        st = engine.state["BTCUSDT"]
        assert st.last_price == pytest.approx(79979.23)
        assert st.mark_change_pct_24h == pytest.approx(0.41)
        assert st.high_24h == pytest.approx(80424.74)
        assert st.quote_volume_24h == pytest.approx(52242942.67)

    async def test_unknown_message_ignored(self, engine):
        """未知消息不抛异常、不影响状态"""
        await engine._on_ws_message(_ws("btcusdt@something", {"e": "?", "x": 1}))
        await engine._on_ws_message({"stream": "", "data": "not-a-dict"})
        assert engine.state["BTCUSDT"].last_price == 0.0

    async def test_trade_callback_fired(self, engine):
        """on_trade 回调被触发"""
        received = []

        async def cb(symbol, tick):
            received.append((symbol, tick))

        engine.on_trade = cb
        await engine._on_ws_message(_ws("btcusdt@trade", AGG_TRADE))
        assert len(received) == 1
        assert received[0][0] == "BTCUSDT"

    async def test_trade_buffered_for_persist(self, engine):
        """成交进入持久化缓冲"""
        await engine._on_ws_message(_ws("btcusdt@trade", AGG_TRADE))
        assert len(engine._trade_buffer["BTCUSDT"]) == 1

    async def test_snapshot_shape(self, engine):
        """快照结构完整(Web 依赖)"""
        await engine._on_ws_message(_ws("btcusdt@trade", AGG_TRADE))
        snap = engine.snapshot("BTCUSDT")
        assert "BTCUSDT" in snap
        s = snap["BTCUSDT"]
        for key in (
            "symbol", "last_price", "best_bid", "best_ask", "mid_price",
            "recent_trades", "recent_klines", "trade_count",
        ):
            assert key in s
        assert s["last_price"] == pytest.approx(79979.50)
