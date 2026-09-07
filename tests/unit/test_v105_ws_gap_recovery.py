"""V10.5: WS 断线重连回补(gap recovery)测试

断线后经 REST 重拉 K线/成交/盘口快照, 幂等合并补齐缺口(跳过旧 / 替换 /
追加), 并刷新数据校验基线。纯合并函数 + resync 集成。
"""

from at20_market.market_engine import MarketDataEngine, merge_klines, merge_trades
from at20_market.market_models import KlineBar, TradeTick


def _kbar(open_time, close=100.0, closed=True):
    return KlineBar(
        symbol="SOLUSDT", interval="1m", open_time=open_time,
        open=close, high=close, low=close, close=close,
        volume=1.0, quote_volume=close, trade_count=1, closed=closed,
    )


def _tick(trade_id, price=100.0):
    return TradeTick(
        trade_id=trade_id, symbol="SOLUSDT", price=price,
        quantity=1.0, quote_quantity=price, is_buyer_maker=False, trade_time=trade_id,
    )


class TestMergeKlines:
    def test_backfills_gap_and_replaces_last(self, db_tables):
        st = _state()
        st.klines.append(_kbar(1000))
        st.klines.append(_kbar(2000))
        added = merge_klines(st, [_kbar(2000, close=105), _kbar(3000), _kbar(4000)])
        assert added == 2  # 3000, 4000 新增; 2000 被替换
        assert [k.open_time for k in st.klines] == [1000, 2000, 3000, 4000]
        assert st.klines[1].close == 105  # 替换后内容更新

    def test_skips_old_bars(self, db_tables):
        st = _state()
        st.klines.append(_kbar(2000))
        added = merge_klines(st, [_kbar(1000), _kbar(2000)])
        assert added == 0
        assert [k.open_time for k in st.klines] == [2000]


class TestMergeTrades:
    def test_only_newer_trade_ids(self, db_tables):
        st = _state()
        st.trades.append(_tick(5))
        added = merge_trades(st, [_tick(3), _tick(5), _tick(6), _tick(7)])
        assert added == 2
        assert [t.trade_id for t in st.trades] == [5, 6, 7]

    def test_empty_state_adds_all(self, db_tables):
        st = _state()
        added = merge_trades(st, [_tick(1), _tick(2)])
        assert added == 2


def _state():
    from at20_market.market_models import SymbolState

    st = SymbolState(symbol="SOLUSDT")
    st.trades = _deque(maxlen=500)
    st.klines = _deque(maxlen=200)
    return st


def _deque(maxlen):
    from collections import deque

    return deque(maxlen=maxlen)


def _kline_array(open_time, close=100.0):
    return [open_time, str(close), str(close), str(close), str(close),
            "1", open_time + 60000, str(close), "1", "0", "0", "0", "0"]


class TestResync:
    async def test_resync_backfills_via_rest(self, db_tables):
        class FakeRest:
            async def get_klines(self, symbol, interval="1m", limit=200):
                return [_kline_array(1000), _kline_array(2000), _kline_array(3000)]

            async def get_agg_trades(self, symbol, limit=500):
                return [
                    {"a": 5, "p": "100.0", "q": "1.0", "m": False, "T": 5},
                    {"a": 6, "p": "101.0", "q": "1.0", "m": False, "T": 6},
                ]

            async def get_depth(self, symbol, limit=20):
                return {"bids": [["99.0", "1.0"]], "asks": [["101.0", "1.0"]]}

        eng = MarketDataEngine(symbols=["SOLUSDT"])
        eng.rest = FakeRest()
        st = eng.state["SOLUSDT"]
        st.klines.append(_kbar(1000))
        st.trades.append(_tick(4))

        await eng.resync()

        assert [k.open_time for k in st.klines] == [1000, 2000, 3000]
        assert [t.trade_id for t in st.trades] == [4, 5, 6]
        assert st.depth.best_bid == 99.0
        assert st.depth.best_ask == 101.0
        # 校验基线刷新到最新 bar
        assert eng._prev_closed["SOLUSDT"].open_time == 3000
