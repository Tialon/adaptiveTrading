"""大单与吸筹检测单元测试"""

from at30_analytics.accumulation import AccumulationDetector
from at30_analytics.whale import WhaleDetector


class TestWhaleDetector:
    def test_static_threshold_hit(self, trade_tick_factory):
        det = WhaleDetector(min_quote=50000.0, quantile=0.99, window=2000)
        # 少于 100 样本:动态阈值不生效,仅静态阈值
        evt = det.update(trade_tick_factory(price=100.0, quantity=1000.0))  # 100k
        assert evt is not None
        assert evt.side == "buy"
        assert evt.quote_quantity == 100000.0

    def test_small_trade_no_event(self, trade_tick_factory):
        det = WhaleDetector(min_quote=50000.0)
        evt = det.update(trade_tick_factory(price=100.0, quantity=1.0))  # 100
        assert evt is None

    def test_sell_side_whale(self, trade_tick_factory):
        det = WhaleDetector(min_quote=50000.0)
        evt = det.update(trade_tick_factory(price=100.0, quantity=1000.0, is_buyer_maker=True))
        assert evt is not None
        assert evt.side == "sell"


class TestAccumulationDetector:
    def test_flat_with_buying_accumulates(self, trade_tick_factory):
        # 工厂是全局的,手动构造以控制时间
        det = AccumulationDetector(window_seconds=60, min_samples=20)
        result = None
        for i in range(50):
            from at20_market.market_models import TradeTick

            tick = TradeTick(
                trade_id=i + 1,
                symbol="BTCUSDT",
                price=100.0,
                quantity=1.0,
                quote_quantity=100.0,
                is_buyer_maker=False,
                trade_time=1_000_000 + i * 1000,
            )
            result = det.update(tick)
        assert result is not None
        assert result.samples >= 20
        assert result.net_flow > 0
        assert result.score >= 0.6
        assert result.is_accumulating

    def test_falling_market_not_accumulating(self):
        from at20_market.market_models import TradeTick

        det = AccumulationDetector(window_seconds=60, min_samples=20)
        result = None
        for i in range(50):
            tick = TradeTick(
                trade_id=i + 1,
                symbol="BTCUSDT",
                price=100.0 + i * 0.5,  # 上涨趋势(非横盘)
                quantity=1.0,
                quote_quantity=100.0 + i * 0.5,
                is_buyer_maker=True,  # 主动卖主导
                trade_time=1_000_000 + i * 1000,
            )
            result = det.update(tick)
        assert not result.is_accumulating

    def test_window_eviction(self):
        from at20_market.market_models import TradeTick

        det = AccumulationDetector(window_seconds=10, min_samples=1)
        old = TradeTick(
            trade_id=1, symbol="BTCUSDT", price=100.0, quantity=1.0,
            quote_quantity=100.0, is_buyer_maker=False, trade_time=1_000_000,
        )
        det.update(old)
        # 窗口外
        new = TradeTick(
            trade_id=2, symbol="BTCUSDT", price=100.0, quantity=1.0,
            quote_quantity=100.0, is_buyer_maker=False, trade_time=1_000_000 + 60_000,
        )
        result = det.update(new)
        assert result.samples == 1
