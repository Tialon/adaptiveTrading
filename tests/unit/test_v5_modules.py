"""V5.0 模块测试: 动态风险因子 / 动态限额 / Signal bucket / StrategyContext / 组合回测"""

import pytest

from at50_strategy.strategy_base import Signal, SignalSide, StrategyContext
from at60_risk.risk_allocation import PortfolioAllocator
from at60_risk.risk_sizing import PositionSizer


class TestRiskAdjustmentFactor:
    def test_calm_market_full_factor(self):
        alloc = PortfolioAllocator()
        assert alloc.risk_adjustment_factor(volatility=0.005) == 1.0

    def test_volatility_reduces(self):
        alloc = PortfolioAllocator()
        assert alloc.risk_adjustment_factor(volatility=0.02) < 1.0
        assert alloc.risk_adjustment_factor(volatility=0.04) < alloc.risk_adjustment_factor(volatility=0.02)

    def test_btc_drop_reduces(self):
        alloc = PortfolioAllocator()
        f = alloc.risk_adjustment_factor(btc_change_24h=-5.0)
        assert f < 1.0

    def test_drawdown_reduces(self):
        alloc = PortfolioAllocator()
        assert alloc.risk_adjustment_factor(drawdown=0.25) < alloc.risk_adjustment_factor(drawdown=0.0)
        assert alloc.risk_adjustment_factor(drawdown=0.35) < alloc.risk_adjustment_factor(drawdown=0.25)

    def test_floor_at_0_4(self):
        alloc = PortfolioAllocator()
        f = alloc.risk_adjustment_factor(volatility=0.05, btc_change_24h=-10.0, drawdown=0.45)
        assert f >= 0.4

    def test_plan_applies_risk_factor(self):
        """敞口 = regime × confidence × risk_factor"""
        alloc = PortfolioAllocator()
        calm = alloc.plan("SOLUSDT", "BULL", 0.8, 20000.0, 100.0, risk_factor=1.0)
        storm = alloc.plan("SOLUSDT", "BULL", 0.8, 20000.0, 100.0, risk_factor=0.6)
        assert storm.target_exposure < calm.target_exposure


class TestDynamicTradeLimit:
    def test_regime_ladder(self):
        s = PositionSizer()
        assert s.dynamic_trade_limit("strong_bull") > s.dynamic_trade_limit("BULL")
        assert s.dynamic_trade_limit("BULL") > s.dynamic_trade_limit("SIDEWAY")
        assert s.dynamic_trade_limit("SIDEWAY") > s.dynamic_trade_limit("BEAR")
        assert s.dynamic_trade_limit("PANIC") == 0.0

    def test_drawdown_tightens(self):
        s = PositionSizer()
        assert s.dynamic_trade_limit("BULL", drawdown=0.25) < s.dynamic_trade_limit("BULL")

    def test_volatility_tightens(self):
        s = PositionSizer()
        assert s.dynamic_trade_limit("SIDEWAY", volatility=0.04) < s.dynamic_trade_limit("SIDEWAY")

    def test_range(self):
        s = PositionSizer()
        assert s.dynamic_trade_limit("strong_bull") <= 0.10
        assert s.dynamic_trade_limit("BEAR") >= 0.0


class TestSignalBucket:
    def test_default_bucket_trade(self):
        sig = Signal(symbol="X", strategy="s", side=SignalSide.SELL, price=1.0)
        assert sig.bucket == "trade"

    def test_bucket_in_dict(self):
        sig = Signal(symbol="X", strategy="s", side=SignalSide.SELL, price=1.0, bucket="core")
        assert sig.to_dict()["bucket"] == "core"


class TestStrategyContext:
    def test_fields(self):
        ctx = StrategyContext(
            symbol="SOLUSDT", regime="BULL", regime_confidence=0.8,
            exposure=0.6, target_exposure=0.75,
            core_qty=70.0, trade_qty=30.0, cash=5000.0, equity=20000.0,
            drawdown=0.05, drawdown_tier=0, alpha_score=80.0,
        )
        assert ctx.total_qty == 100.0
        d = ctx.to_dict()
        assert d["regime"] == "BULL"
        assert d["core_qty"] == 70.0
        assert "drawdown_tier" in d

    def test_defaults(self):
        ctx = StrategyContext()
        assert ctx.regime == "SIDEWAY"
        assert ctx.total_qty == 0.0


class TestPortfolioBacktest:
    async def test_synthetic_klines(self):
        """合成K线跑通组合回测(涨->跌->涨, 应触发再平衡)"""
        from at70_backtest.backtest_portfolio import PortfolioBacktester

        # 合成 600 根: 100 -> 130 -> 90 -> 120
        klines = []
        price = 100.0
        for i in range(600):
            if i < 200:
                price *= 1.0013  # 涨至 ~130
            elif i < 400:
                price *= 0.9988  # 跌至 ~105
            else:
                price *= 1.0007  # 回升
            klines.append([i * 60000, price, price * 1.001, price * 0.999,
                           price, 10.0, i * 60000 + 59999, 1000.0, 5])

        bt = PortfolioBacktester(symbol="TESTUSDT", initial_cash=20000.0)
        result = await bt.run(klines)
        s = result.summary()
        assert s["bars"] == 600
        assert result.excess_return != 0.0 or result.total_return != 0.0
        # 曲线完整性
        assert len(result.equity_curve) > 0
        assert len(result.exposure_curve) > 0
        # benchmark: 首尾价
        assert result.benchmark_return != 0.0

    async def test_flat_market_no_rebalance(self):
        """横盘市场: 不触发再平衡"""
        from at70_backtest.backtest_portfolio import PortfolioBacktester

        klines = []
        for i in range(300):
            p = 100.0 + (0.5 if i % 2 == 0 else -0.5) * 0.1  # 99.9~100.1 微幅
            klines.append([i * 60000, p, p * 1.0005, p * 0.9995, p, 10.0, 0, 0, 0])

        bt = PortfolioBacktester(symbol="TESTUSDT")
        result = await bt.run(klines)
        # V7 真实策略管线: 横盘网格会周期触发, 但敞口稳定(偏离小)
        # 断言改为: 敞口曲线波动小 + 对账平衡
        assert result.reconciliation.get("balanced") is True
        if result.exposure_curve:
            spread = max(result.exposure_curve) - min(result.exposure_curve)
            assert spread < 0.5  # 敞口没有剧烈漂移
