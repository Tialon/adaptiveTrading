"""指标单元测试:VWAP / Delta / CVD"""

from at20_analytics.indicators import CVDTracker, DeltaTracker, VWAPCalculator


class TestVWAP:
    def test_flat_price_vwap_equals_price(self, trade_tick_factory):
        vwap = VWAPCalculator()
        for _ in range(10):
            r = vwap.update(100.0, 2.0)
        assert abs(r.vwap - 100.0) < 1e-9
        assert r.upper_band >= r.vwap >= r.lower_band

    def test_volume_weighted(self):
        vwap = VWAPCalculator()
        # 2手@100 + 1手@200 => vwap = (200+200)/3 = 133.33
        vwap.update(100.0, 2.0)
        r = vwap.update(200.0, 1.0)
        assert abs(r.vwap - 400.0 / 3.0) < 1e-9

    def test_deviation_of(self):
        vwap = VWAPCalculator()
        for _ in range(20):
            vwap.update(100.0, 1.0)
        r = vwap.deviation_of(101.0)
        assert abs(r.deviation - 0.01) < 1e-6

    def test_window_eviction(self):
        vwap = VWAPCalculator(window=5)
        for _ in range(5):
            vwap.update(100.0, 1.0)
        for _ in range(5):
            r = vwap.update(200.0, 1.0)
        # 窗口内全是 200
        assert abs(r.vwap - 200.0) < 1e-9


class TestDelta:
    def test_all_buys(self, trade_tick_factory):
        delta = DeltaTracker()
        r = delta.update(trade_tick_factory(is_buyer_maker=False))
        assert r.delta_ratio == 1.0
        assert r.delta > 0

    def test_all_sells(self, trade_tick_factory):
        delta = DeltaTracker()
        r = delta.update(trade_tick_factory(is_buyer_maker=True))
        assert r.delta_ratio == -1.0

    def test_mixed(self, trade_tick_factory):
        delta = DeltaTracker()
        delta.update(trade_tick_factory(quantity=1.0, is_buyer_maker=False))
        r = delta.update(trade_tick_factory(quantity=3.0, is_buyer_maker=True))
        assert r.delta_ratio == -0.5


class TestCVD:
    def test_accumulates(self, trade_tick_factory):
        cvd = CVDTracker()
        r = cvd.update(trade_tick_factory(quantity=10.0, price=1.0, is_buyer_maker=False))
        r = cvd.update(trade_tick_factory(quantity=5.0, price=1.0, is_buyer_maker=True))
        assert r.cvd == 5.0  # +10 - 5

    def test_rising_falling(self, trade_tick_factory):
        cvd = CVDTracker()
        for _ in range(30):
            r = cvd.update(trade_tick_factory(quantity=1.0, price=1.0, is_buyer_maker=False))
        assert r.rising and not r.falling
        for _ in range(30):
            r = cvd.update(trade_tick_factory(quantity=1.0, price=1.0, is_buyer_maker=True))
        assert r.falling

    def test_reset(self, trade_tick_factory):
        cvd = CVDTracker()
        cvd.update(trade_tick_factory(quantity=10.0, price=1.0))
        cvd.reset()
        r = cvd.update(trade_tick_factory(quantity=1.0, price=1.0))
        assert r.cvd == 1.0
