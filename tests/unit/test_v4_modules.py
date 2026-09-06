"""V4.0 模块单元测试: Allocation / Buckets / Sizing / TieredDrawdown / Journal"""

import pytest

from at60_risk.risk_allocation import EXPOSURE_TABLE, PortfolioAllocator
from at60_risk.risk_buckets import BucketPositionManager, CORE, TRADE
from at60_risk.risk_position import PositionManager
from at60_risk.risk_sizing import PositionSizer
from at60_risk.risk_tiered import TIERS, TieredDrawdownManager


class TestPortfolioAllocator:
    def test_exposure_table_ordering(self):
        """敞口表单调: 牛 > 震荡 > 熊 > 恐慌"""
        assert EXPOSURE_TABLE["strong_bull"] > EXPOSURE_TABLE["BULL"]
        assert EXPOSURE_TABLE["BULL"] > EXPOSURE_TABLE["SIDEWAY"]
        assert EXPOSURE_TABLE["SIDEWAY"] > EXPOSURE_TABLE["BEAR"]
        assert EXPOSURE_TABLE["BEAR"] > EXPOSURE_TABLE["PANIC"]

    def test_bull_high_exposure(self):
        """牛市 80-90% 仓位目标"""
        alloc = PortfolioAllocator()
        plan = alloc.plan(
            symbol="SOLUSDT", regime="strong_bull", confidence=0.9,
            equity=20000.0, market_price=100.0,
        )
        assert plan.target_exposure >= 0.75  # 高置信强牛 -> 75%+

    def test_bear_low_exposure(self):
        """熊市 20-30% 仓位"""
        alloc = PortfolioAllocator()
        plan = alloc.plan(
            symbol="SOLUSDT", regime="BEAR", confidence=0.8,
            equity=20000.0, market_price=100.0,
        )
        assert 0.15 <= plan.target_exposure <= 0.35

    def test_confidence_weighting(self):
        """低置信向 50% 收敛"""
        alloc = PortfolioAllocator()
        high = alloc.target_exposure("BULL", 0.9)
        low = alloc.target_exposure("BULL", 0.3)
        assert high > low > 0.5

    def test_refine_strong_bull(self):
        alloc = PortfolioAllocator()
        assert alloc.refine_regime("BULL", 0.8) == "strong_bull"
        assert alloc.refine_regime("BULL", 0.5) == "BULL"
        assert alloc.refine_regime("BEAR", 0.9) == "BEAR"

    def test_dual_bucket_split(self):
        """双仓拆分: 核心 70% / 交易 30%"""
        alloc = PortfolioAllocator()
        plan = alloc.plan(
            symbol="SOLUSDT", regime="SIDEWAY", confidence=0.6,
            equity=10000.0, market_price=100.0,
        )
        total = plan.target_core_qty + plan.target_trade_qty
        assert plan.target_core_qty == pytest.approx(total * 0.7)
        assert plan.target_trade_qty == pytest.approx(total * 0.3)

    def test_rebalance_tolerance(self):
        """敞口偏离超容忍带 -> rebalance_needed"""
        alloc = PortfolioAllocator()
        # 目标 50%, 当前 100 SOL@100 = 10000(权益 20000) -> 敞口 50% 无偏离
        plan_ok = alloc.plan(
            "SOLUSDT", "SIDEWAY", 0.5, 20000.0, 100.0,
            current_core_qty=70.0, current_trade_qty=30.0,
        )
        assert not plan_ok.rebalance_needed
        # 当前 180 SOL -> 敞口 90%, 偏离 40%
        plan_far = alloc.plan(
            "SOLUSDT", "SIDEWAY", 0.5, 20000.0, 100.0,
            current_core_qty=130.0, current_trade_qty=50.0,
        )
        assert plan_far.rebalance_needed

    def test_bear_no_add(self):
        """熊市不加仓: core_diff 归零"""
        alloc = PortfolioAllocator()
        plan = alloc.plan(
            "SOLUSDT", "BEAR", 0.8, 20000.0, 100.0,
            current_core_qty=10.0, current_trade_qty=5.0,
        )
        assert plan.core_diff == 0.0
        assert plan.trade_diff == 0.0
        assert any("停止加仓" in r for r in plan.reasons)


class TestBucketPositionManager:
    def test_buy_into_trade_bucket(self):
        pm = PositionManager()
        bm = BucketPositionManager(pm)
        bm.on_buy_fill("SOLUSDT", 10.0, 100.0, TRADE)
        assert bm.trade("SOLUSDT") == 10.0
        assert bm.core("SOLUSDT") == 0.0
        assert bm.total("SOLUSDT") == 10.0
        # 总账同步
        assert pm.get("SOLUSDT").quantity == 10.0

    def test_buy_into_core_bucket(self):
        pm = PositionManager()
        bm = BucketPositionManager(pm)
        bm.on_buy_fill("SOLUSDT", 70.0, 100.0, CORE)
        assert bm.core("SOLUSDT") == 70.0
        assert bm.trade("SOLUSDT") == 0.0

    def test_sell_trade_bucket_only(self):
        """卖交易仓不动核心仓"""
        pm = PositionManager()
        bm = BucketPositionManager(pm)
        bm.on_buy_fill("SOLUSDT", 70.0, 100.0, CORE)
        bm.on_buy_fill("SOLUSDT", 30.0, 100.0, TRADE)

        realized, used = bm.on_sell_fill("SOLUSDT", 20.0, 120.0, TRADE)
        assert used == TRADE
        assert realized == pytest.approx(20.0 * 20.0)  # (120-100)*20
        assert bm.core("SOLUSDT") == 70.0  # 核心仓未动
        assert bm.trade("SOLUSDT") == 10.0

    def test_sell_rejects_crossing_into_core(self):
        """交易仓不足 -> 拒绝(保护核心仓)"""
        pm = PositionManager()
        bm = BucketPositionManager(pm)
        bm.on_buy_fill("SOLUSDT", 70.0, 100.0, CORE)
        bm.on_buy_fill("SOLUSDT", 5.0, 100.0, TRADE)

        realized, used = bm.on_sell_fill("SOLUSDT", 10.0, 120.0, TRADE)  # 要 10 只有 5
        assert used == "REJECTED"
        assert bm.core("SOLUSDT") == 70.0
        assert bm.trade("SOLUSDT") == 5.0

    def test_sell_core_explicit(self):
        """显式卖核心仓(仅 allocation 引导)"""
        pm = PositionManager()
        bm = BucketPositionManager(pm)
        bm.on_buy_fill("SOLUSDT", 70.0, 100.0, CORE)
        realized, used = bm.on_sell_fill("SOLUSDT", 20.0, 110.0, CORE)
        assert used == CORE
        assert bm.core("SOLUSDT") == 50.0
        assert realized == pytest.approx(20.0 * 10.0)

    def test_view(self):
        pm = PositionManager()
        bm = BucketPositionManager(pm)
        bm.on_buy_fill("SOLUSDT", 70.0, 100.0, CORE)
        bm.on_buy_fill("SOLUSDT", 30.0, 110.0, TRADE)
        v = bm.view("SOLUSDT")
        assert v["core_qty"] == 70.0 and v["trade_qty"] == 30.0
        assert v["core_avg_cost"] == 100.0
        assert v["trade_avg_cost"] == 110.0


class TestPositionSizer:
    def test_bull_high_score_buys_more(self):
        s = PositionSizer()
        bull = s.size(decision_score=90, alpha_score=85, regime="BULL",
                      equity=20000.0, price=100.0)
        assert bull["quote"] > 0
        assert bull["quote"] <= 20000 * 0.05  # 单笔上限 5%

    def test_bear_shrinks_position(self):
        """同样的分, 熊市买得少(85分牛 10% vs 熊 2%)"""
        s = PositionSizer()
        bull = s.size(85, 85, "BULL", 20000.0, 100.0)
        bear = s.size(85, 85, "BEAR", 20000.0, 100.0)
        assert bear["quote"] < bull["quote"]
        assert bear["quote"] < bull["quote"] / 2

    def test_panic_zero(self):
        s = PositionSizer()
        r = s.size(90, 90, "PANIC", 20000.0, 100.0)
        assert r["quote"] == 0.0

    def test_tiered_factor_zero_blocks(self):
        """回撤 level3+ 加仓系数 0"""
        s = PositionSizer()
        r = s.size(90, 90, "BULL", 20000.0, 100.0, tiered_factor=0.0)
        assert r["quote"] == 0.0

    def test_exposure_room_cap(self):
        """敞口缺口限制买入量"""
        s = PositionSizer()
        r = s.size(90, 90, "BULL", 20000.0, 100.0, exposure_room_quote=100.0)
        assert r["quote"] <= 100.0

    def test_min_notional(self):
        s = PositionSizer()
        r = s.size(30, 30, "BEAR", 20000.0, 100.0)
        if r["quote"] == 0:
            assert "过小" in r["detail"]


class TestTieredDrawdown:
    def test_level_escalation(self):
        m = TieredDrawdownManager()
        assert m.evaluate(0.05) is None  # 正常
        t1 = m.evaluate(0.12)
        assert t1.level == 1 and t1.name == "reduce_trade"
        t2 = m.evaluate(0.22)
        assert t2.level == 2
        t5 = m.evaluate(0.55)
        assert t5.level == 5 and t5.name == "emergency"

    def test_factors_tighten(self):
        m = TieredDrawdownManager()
        assert m.size_factor == 1.0
        m.evaluate(0.12)  # L1
        assert m.size_factor == pytest.approx(0.7)
        m.evaluate(0.35)  # L3
        assert m.size_factor == 0.0
        assert m.exposure_factor == pytest.approx(0.5)

    def test_hysteresis_no_flapping(self):
        """阈值附近不抖动: 升到 L2 后 19% 不立即降档"""
        m = TieredDrawdownManager()
        m.evaluate(0.21)  # L2
        m.evaluate(0.195)  # 略低于 20% 但未到 18%
        assert m.current_level == 2
        m.evaluate(0.17)  # 低于 18% -> 降 L1
        assert m.current_level == 1

    def test_recovery(self):
        m = TieredDrawdownManager()
        m.evaluate(0.25)  # L2
        m.evaluate(0.05)  # 恢复
        assert m.current_level == 0
        assert m.size_factor == 1.0

    def test_hard_breaker_at_level5(self):
        class FakeBreaker:
            def __init__(self):
                self.tripped = False

            def manual_trip(self, reason):
                self.tripped = True

        fb = FakeBreaker()
        m = TieredDrawdownManager(hard_breaker=fb)
        m.evaluate(0.55)
        assert fb.tripped  # 50% 触发熔断

    def test_status_shape(self):
        m = TieredDrawdownManager()
        s = m.status()
        assert s["level"] == 0 and s["size_factor"] == 1.0


class TestDecisionJournal:
    async def test_log_and_read(self, db_tables):
        from at50_strategy.strategy_journal import DecisionJournal

        j = DecisionJournal()
        await j.log(
            symbol="SOLUSDT", action="BUY", price=100.0, quantity=5.0,
            regime="BULL", regime_confidence=0.8, alpha_score=75.0,
            decision_score=85.0, core_qty=70.0, trade_qty=10.0,
            cash=8000.0, equity=20000.0, reason="多策略看多",
            context={"vwap": 101.0},
        )
        rows = await j.recent("SOLUSDT")
        assert len(rows) == 1
        r = rows[0]
        assert r["action"] == "BUY"
        assert r["regime"] == "BULL"
        assert r["alpha_score"] == 75.0
        assert r["core_qty"] == 70.0
        assert r["equity"] == 20000.0

    async def test_empty_recent(self, db_tables):
        from at50_strategy.strategy_journal import DecisionJournal

        j = DecisionJournal()
        rows = await j.recent("SOLUSDT")
        assert rows == []
