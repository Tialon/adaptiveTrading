"""策略单元测试:买入 / 卖出 / 网格 / 趋势"""

from analytics.engine import MarketAnalytics
from strategy.buy import BuyStrategy
from strategy.grid import GridStrategy
from strategy.sell import SellStrategy
from strategy.trend import TrendStrategy


def make_analytics(
    symbol="BTCUSDT",
    price=100.0,
    vwap=101.0,
    vwap_deviation=None,
    accumulating=False,
    acc_score=0.0,
    cvd_rising=False,
    cvd_falling=False,
    trend="neutral",
    ema_fast=None,
    ema_slow=None,
):
    if vwap_deviation is None:
        vwap_deviation = (price - vwap) / vwap
    return MarketAnalytics(
        symbol=symbol,
        price=price,
        vwap=vwap,
        vwap_deviation=vwap_deviation,
        accumulation=acc_score,
        is_accumulating=accumulating,
        cvd_rising=cvd_rising,
        cvd_falling=cvd_falling,
        trend=trend,
        ema_fast=ema_fast if ema_fast is not None else price,
        ema_slow=ema_slow if ema_slow is not None else price,
    )


class TestBuyStrategy:
    def test_accumulation_and_dip_buys(self):
        s = BuyStrategy()
        a = make_analytics(
            price=100.0, vwap=100.6, vwap_deviation=-0.006,
            accumulating=True, acc_score=0.8, cvd_rising=True, trend="neutral",
        )
        signals = s.on_market(a)
        assert len(signals) == 1
        assert signals[0].side.value == "BUY"
        assert signals[0].quote_amount > 0

    def test_no_signal_on_uptrend_no_dip(self):
        s = BuyStrategy()
        a = make_analytics(price=100.0, vwap=99.0, vwap_deviation=0.01, trend="up")
        assert s.on_market(a) == []

    def test_cooldown(self):
        s = BuyStrategy()
        s.signal_cooldown = 60.0
        a = make_analytics(
            price=100.0, vwap=100.6, vwap_deviation=-0.006,
            accumulating=True, acc_score=0.8, cvd_rising=True,
        )
        assert len(s.on_market(a)) == 1
        # 冷却期内不再发信号
        assert s.on_market(a) == []


class TestSellStrategy:
    def test_take_profit(self):
        s = SellStrategy()
        s.position_provider = lambda sym: (1.0, 100.0, 100.0)
        a = make_analytics(price=101.5)  # +1.5% > 1%
        signals = s.on_market(a)
        assert len(signals) == 1
        assert signals[0].side.value == "SELL"
        assert signals[0].quantity == 1.0

    def test_trailing_drawdown(self):
        s = SellStrategy()
        # 均价100 峰值105 现价101.6 -> 回撤 3.2% > 0.6% 且浮盈1.6%
        s.position_provider = lambda sym: (1.0, 100.0, 105.0)
        a = make_analytics(price=101.6)
        signals = s.on_market(a)
        assert len(signals) == 1
        assert "回撤" in signals[0].reason

    def test_no_position_no_signal(self):
        s = SellStrategy()
        s.position_provider = lambda sym: None
        assert s.on_market(make_analytics(price=200.0)) == []

    def test_trend_reversal_sell(self):
        s = SellStrategy()
        s.position_provider = lambda sym: (1.0, 100.0, 100.0)
        a = make_analytics(price=100.1, trend="down", cvd_falling=True)
        signals = s.on_market(a)
        assert len(signals) == 1
        assert "趋势" in signals[0].reason


class TestGridStrategy:
    def test_grid_creation(self):
        g = GridStrategy()
        grid = g.ensure_grid("BTCUSDT", 100.0)
        assert grid.lower < 100.0 < grid.upper
        assert len(grid.levels) == grid.count + 1

    def test_price_drop_triggers_buy(self):
        g = GridStrategy()
        g.ensure_grid("BTCUSDT", 100.0)
        grid = g.get_grid("BTCUSDT")
        step = grid.step
        # 价格跌破第一层
        target = grid.levels[0].price - step * 0.5
        a = make_analytics(price=target)
        signals = g.on_market(a)
        buys = [s for s in signals if s.side.value == "BUY"]
        assert len(buys) >= 1

    def test_price_rise_triggers_sell_after_buy(self):
        g = GridStrategy()
        g.ensure_grid("BTCUSDT", 100.0)
        grid = g.get_grid("BTCUSDT")
        step = grid.step
        # 先跌破 L0 买入
        g.on_market(make_analytics(price=grid.levels[0].price - step * 0.5))
        held = [lv for lv in g.get_grid("BTCUSDT").levels if lv.held]
        assert len(held) >= 1
        # 价格回升超过 L0 + step/2 卖出
        signals = g.on_market(make_analytics(price=grid.levels[0].price + step * 0.9))
        sells = [s for s in signals if s.side.value == "SELL"]
        assert len(sells) >= 1
        assert not any(lv.held for lv in g.get_grid("BTCUSDT").levels if lv.price == grid.levels[0].price)

    def test_reset_on_breakout(self):
        g = GridStrategy()
        g.ensure_grid("BTCUSDT", 100.0)
        old_upper = g.get_grid("BTCUSDT").upper
        # 价格暴涨越界
        g.on_market(make_analytics(price=old_upper * 1.01))
        assert g.get_grid("BTCUSDT").upper > old_upper


class TestTrendStrategy:
    def test_golden_cross_buy(self):
        t = TrendStrategy()
        # 初始 neutral,转 up
        t._last_trend["BTCUSDT"] = "neutral"
        a = make_analytics(price=100.0, trend="up", cvd_rising=True)
        signals = t.on_market(a)
        assert len(signals) == 1
        assert signals[0].side.value == "BUY"

    def test_death_cross_sell(self):
        t = TrendStrategy()
        t._last_trend["BTCUSDT"] = "up"
        a = make_analytics(price=100.0, trend="down", cvd_falling=True)
        signals = t.on_market(a)
        assert len(signals) == 1
        assert signals[0].side.value == "SELL"

    def test_no_repeat_signal(self):
        t = TrendStrategy()
        t._last_trend["BTCUSDT"] = "up"
        a = make_analytics(price=100.0, trend="up", cvd_rising=True)
        # up -> up 不发信号
        assert t.on_market(a) == []
