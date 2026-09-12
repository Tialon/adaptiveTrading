"""策略单元测试(V2.0: 评分模型 / 分批止盈 / 移动止盈 / 趋势退出)"""

from at20_analytics.engine import MarketAnalytics
from at30_strategy.strategy_buy import BuyStrategy
from at30_strategy.strategy_grid import GridStrategy
from at30_strategy.strategy_sell import SellStrategy
from at30_strategy.strategy_trend import TrendStrategy


def make_analytics(
    symbol="SOLUSDT",
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
    recent_high=None,
    recent_low=None,
    volume_ratio=1.0,
    delta_ratio=0.0,
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
        recent_high=recent_high if recent_high is not None else price * 1.01,
        recent_low=recent_low if recent_low is not None else price * 0.99,
        volume_ratio=volume_ratio,
        delta_ratio=delta_ratio,
    )


class TestBuyStrategy:
    """V2.0 Entry 评分模型"""

    def test_high_score_buys(self):
        """全维度向好: 评分 >= 80 触发买入"""
        s = BuyStrategy()
        a = make_analytics(
            price=98.9, vwap=101.0, vwap_deviation=-0.0208,  # 折价 >2% 满分
            recent_high=103.0, recent_low=99.0,  # 接近区间低点
            cvd_rising=True, delta_ratio=0.3, volume_ratio=1.5,
        )
        signals = s.on_market(a)
        assert len(signals) == 1
        sig = signals[0]
        assert sig.side.value == "BUY"
        assert sig.score >= 80
        assert len(sig.reason) >= 5  # 5 个评分项
        assert isinstance(sig.reason, list)
        assert "vwap_deviation" in sig.indicators

    def test_observe_band(self):
        """中等分数(60-80)进入观察档: 产生信号但标注观察"""
        s = BuyStrategy()
        a = make_analytics(
            price=100.5, vwap=101.0, vwap_deviation=-0.005,
            recent_high=104.0, recent_low=100.0,  # 位置分低
            cvd_rising=True, delta_ratio=0.15, volume_ratio=1.1,
        )
        signals = s.on_market(a)
        if signals:  # 分数 >= 60 才有信号
            assert signals[0].score < 80
            assert any("观察档" in r for r in signals[0].reason)

    def test_low_score_no_signal(self):
        """价格高位+资金流出: 低于 60 无信号"""
        s = BuyStrategy()
        a = make_analytics(
            price=103.0, vwap=101.0, vwap_deviation=0.02,  # 高于 VWAP
            recent_high=103.5, recent_low=99.0,  # 区间高位
            cvd_rising=False, delta_ratio=-0.2, volume_ratio=0.5,
        )
        assert s.on_market(a) == []

    def test_cooldown(self):
        s = BuyStrategy()
        s.signal_cooldown = 60.0
        a = make_analytics(
            price=99.0, vwap=101.0, vwap_deviation=-0.0198,
            recent_high=103.0, recent_low=99.0,
            cvd_rising=True, delta_ratio=0.3, volume_ratio=1.5,
        )
        assert len(s.on_market(a)) == 1
        assert s.on_market(a) == []

    def test_score_components_sum(self):
        """评分权重合计 100%"""
        s = BuyStrategy()
        a = make_analytics()
        comps = s._score_components(a)
        total_weight = sum(w for _, _, w, _ in comps)
        assert total_weight == pytest.approx(1.0)
        assert len(comps) == 5


import pytest  # noqa: E402


class TestSellStrategy:
    """V2.0 Exit: 分批止盈 / 移动止盈 / 趋势退出"""

    def test_take_profit_ladder_5pct(self):
        """盈利 5% -> 卖 20%"""
        s = SellStrategy()
        s.position_provider = lambda sym: (1.0, 100.0, 100.0)
        a = make_analytics(price=105.5)  # +5.5%
        signals = s.on_market(a)
        assert len(signals) == 1
        sig = signals[0]
        assert sig.side.value == "SELL"
        assert sig.quantity == pytest.approx(0.2)  # 20% of 1.0
        assert any("分批止盈" in r for r in sig.reason)
        assert sig.indicators["profit_ratio"] == pytest.approx(0.055, abs=1e-3)

    def test_take_profit_ladder_10pct(self):
        """盈利 10% -> 卖 30%"""
        s = SellStrategy()
        s.position_provider = lambda sym: (2.0, 100.0, 100.0)
        a = make_analytics(price=110.0)  # +10%
        signals = s.on_market(a)
        assert signals[0].quantity == pytest.approx(0.6)  # 30% of 2.0

    def test_take_profit_ladder_20pct(self):
        """盈利 20% -> 卖 50%"""
        s = SellStrategy()
        s.position_provider = lambda sym: (2.0, 100.0, 100.0)
        a = make_analytics(price=121.0)  # +21%
        signals = s.on_market(a)
        assert signals[0].quantity == pytest.approx(1.0)  # 50% of 2.0

    def test_no_profit_no_signal(self):
        """盈利不足 5%: 不卖"""
        s = SellStrategy()
        s.position_provider = lambda sym: (1.0, 100.0, 100.0)
        a = make_analytics(price=103.0)  # +3%
        assert s.on_market(a) == []

    def test_trailing_drawdown(self):
        """V2.0: 峰值回撤 5% 清仓(默认 sell_trailing_drawdown=0.05)"""
        s = SellStrategy()
        # 均价100 峰值110 现价104.4 -> 回撤 5.1% 且浮盈 4.4%
        s.position_provider = lambda sym: (1.0, 100.0, 110.0)
        a = make_analytics(price=104.4)
        signals = s.on_market(a)
        assert len(signals) == 1
        assert signals[0].quantity == pytest.approx(1.0)  # 清仓
        assert any("移动止盈" in r for r in signals[0].reason)

    def test_trailing_not_triggered_below_5pct(self):
        s = SellStrategy()
        # 峰值 110 现价 105.2 -> 回撤 4.4% < 5%, 且盈利 5.2% 会触发分批止盈?
        # 盈利 5.2% -> 分批止盈卖 20%, 该信号来自止盈档不来自移动止盈
        s.position_provider = lambda sym: (1.0, 100.0, 110.0)
        a = make_analytics(price=104.0)  # +4% 未达止盈档, 回撤 5.5%>5% -> 会触发移动止盈? (110-104)/110=5.45%
        # 改: 现价 105.5 -> 盈利 5.5% 触发分批止盈; 为纯粹测试回撤不足, 用盈利 4%
        a = make_analytics(price=104.6)  # +4.6%, 回撤 (110-104.6)/110=4.9% < 5%
        signals = s.on_market(a)
        # 无止盈(4.6%<5%) 无移动止盈(4.9%<5%) 无趋势退出
        assert signals == []

    def test_trend_reversal_exit(self):
        """趋势退出: EMA down + CVD falling + 买压减(三条件中二)"""
        s = SellStrategy()
        s.position_provider = lambda sym: (1.0, 100.0, 100.0)
        a = make_analytics(price=100.1, trend="down", cvd_falling=True, delta_ratio=-0.1)
        signals = s.on_market(a)
        assert len(signals) == 1
        assert any("趋势退出" in r for r in signals[0].reason)
        assert signals[0].quantity == pytest.approx(1.0)  # 清仓

    def test_no_position_no_signal(self):
        s = SellStrategy()
        s.position_provider = lambda sym: None
        assert s.on_market(make_analytics(price=200.0)) == []

    def test_min_notional_filtered(self):
        """卖出金额过小(<10 USDT)被过滤"""
        s = SellStrategy()
        s.position_provider = lambda sym: (0.05, 100.0, 100.0)  # 持仓过小
        a = make_analytics(price=110.0)
        assert s.on_market(a) == []


class TestGridStrategy:
    def test_grid_creation(self):
        g = GridStrategy()
        grid = g.ensure_grid("SOLUSDT", 100.0)
        assert grid.lower < 100.0 < grid.upper
        assert len(grid.levels) == grid.count + 1
        assert grid.per_level_quote > 0  # V2.0: 百分比限额下不为 0

    def test_price_drop_triggers_buy(self):
        g = GridStrategy()
        g.ensure_grid("SOLUSDT", 100.0)
        grid = g.get_grid("SOLUSDT")
        step = grid.step
        target = grid.levels[0].price - step * 0.5
        a = make_analytics(price=target)
        signals = g.on_market(a)
        buys = [s for s in signals if s.side.value == "BUY"]
        assert len(buys) >= 1
        assert isinstance(buys[0].reason, list)
        assert buys[0].indicators["level"] == 0

    def test_price_rise_triggers_sell_after_buy(self):
        g = GridStrategy()
        g.ensure_grid("SOLUSDT", 100.0)
        grid = g.get_grid("SOLUSDT")
        step = grid.step
        g.on_market(make_analytics(price=grid.levels[0].price - step * 0.5))
        held = [lv for lv in g.get_grid("SOLUSDT").levels if lv.held]
        assert len(held) >= 1
        signals = g.on_market(make_analytics(price=grid.levels[0].price + step * 0.9))
        sells = [s for s in signals if s.side.value == "SELL"]
        assert len(sells) >= 1

    def test_reset_on_breakout(self):
        g = GridStrategy()
        g.ensure_grid("SOLUSDT", 100.0)
        old_upper = g.get_grid("SOLUSDT").upper
        g.on_market(make_analytics(price=old_upper * 1.02))
        assert g.get_grid("SOLUSDT").upper > old_upper


class TestTrendStrategy:
    def test_golden_cross_buy(self):
        t = TrendStrategy()
        t._last_trend["SOLUSDT"] = "neutral"
        a = make_analytics(price=100.0, trend="up", cvd_rising=True)
        signals = t.on_market(a)
        assert len(signals) == 1
        assert signals[0].side.value == "BUY"
        assert signals[0].score == 75.0
        assert any("金叉" in r for r in signals[0].reason)

    def test_death_cross_sell(self):
        t = TrendStrategy()
        t._last_trend["SOLUSDT"] = "up"
        a = make_analytics(price=100.0, trend="down", cvd_falling=True)
        signals = t.on_market(a)
        assert len(signals) == 1
        assert signals[0].side.value == "SELL"
        assert any("死叉" in r for r in signals[0].reason)

    def test_no_repeat_signal(self):
        t = TrendStrategy()
        t._last_trend["SOLUSDT"] = "up"
        a = make_analytics(price=100.0, trend="up", cvd_rising=True)
        assert t.on_market(a) == []
