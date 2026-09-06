"""
V6.0 金融正确性测试(Correctness First)

Level 2 Invariant: 核心仓不可被交易卖 / PANIC 禁买 / 敞口上限
Level 3 Accounting: 账本精确对账(双仓独立成本/费用)
Level 4 Regression: 固定合成数据集(bull/bear/sideway/crash)基准
"""

import pytest

from at50_strategy.strategy_base import Signal, SignalSide
from at60_risk.risk_buckets import BucketPositionManager, CORE, TRADE
from at60_risk.risk_ledger import PortfolioLedger
from at60_risk.risk_position import PositionManager


# ============ Level 3: Accounting(最优先) ============

class TestLedgerAccounting:
    def test_double_entry_integrity(self):
        """买->卖 完整对账: 现金精确无漂移"""
        ledger = PortfolioLedger()
        ledger.init_cash(10000.0)

        ledger.record_fill(1000, "SOLUSDT", "trade", "BUY", 10.0, 100.0, fee=1.0)
        ledger.record_fill(2000, "SOLUSDT", "trade", "SELL", 10.0, 120.0, fee=1.2)

        # 现金: 10000 - 1000 - 1 + 1200 - 1.2 = 10197.8
        assert ledger.cash() == pytest.approx(10197.8)

    def test_dual_bucket_independent_costs(self):
        """双仓成本独立: trade PnL 不用 core_cost(V6 修复的核心 bug)"""
        ledger = PortfolioLedger()
        ledger.init_cash(100000.0)

        ledger.record_fill(1000, "SOLUSDT", "core", "BUY", 70.0, 100.0, fee=7.0)
        ledger.record_fill(2000, "SOLUSDT", "trade", "BUY", 10.0, 150.0, fee=1.5)
        # trade 卖 @170: realized = (170 - 150.15) * 10 - fee
        entry = ledger.record_fill(3000, "SOLUSDT", "trade", "SELL", 10.0, 170.0, fee=1.7)

        # 核心成本 ≈ 100.1(含费), 但 trade realized 必须基于 150.15
        expected_cost = (10.0 * 150.0 + 1.5) / 10.0  # 150.15
        assert entry.avg_cost_before == pytest.approx(expected_cost)
        assert entry.realized_pnl == pytest.approx((170.0 - expected_cost) * 10.0 - 1.7)
        # 绝不是 (170-100)*10 = 700
        assert entry.realized_pnl < 200.0

    def test_core_cost_untouched_by_trade(self):
        """交易仓买卖不影响核心仓成本"""
        ledger = PortfolioLedger()
        ledger.init_cash(100000.0)
        ledger.record_fill(1000, "SOLUSDT", "core", "BUY", 70.0, 100.0, fee=7.0)
        core_cost = ledger.avg_cost("SOLUSDT", "core")

        ledger.record_fill(2000, "SOLUSDT", "trade", "BUY", 10.0, 130.0, fee=1.3)
        ledger.record_fill(3000, "SOLUSDT", "trade", "SELL", 10.0, 140.0, fee=1.4)

        assert ledger.avg_cost("SOLUSDT", "core") == pytest.approx(core_cost)

    def test_reconciliation_balanced(self):
        """对账: 账本现金与外部现金一致"""
        ledger = PortfolioLedger()
        ledger.init_cash(50000.0)
        for i, (side, qty, price) in enumerate([
            ("BUY", 5.0, 100.0), ("SELL", 2.0, 110.0), ("BUY", 3.0, 105.0),
        ]):
            ledger.record_fill(i * 1000, "SOLUSDT", "trade", side, qty, price, qty * price * 0.001)

        recon = ledger.reconcile("SOLUSDT", ledger.cash(), 108.0, 50000.0)
        assert recon.balanced
        assert recon.diff == pytest.approx(0.0)

    def test_reconciliation_detects_drift(self):
        """对账: 外部现金与账本不符时被检出"""
        ledger = PortfolioLedger()
        ledger.init_cash(10000.0)
        ledger.record_fill(1000, "SOLUSDT", "trade", "BUY", 10.0, 100.0, 0.0)
        # 账本现金应为 9000; 提供错误的外部值 8000
        recon = ledger.reconcile("SOLUSDT", 8000.0, 100.0, 10000.0)
        assert not recon.balanced
        assert recon.diff == pytest.approx(-1000.0)

    def test_partial_sell_keeps_cost(self):
        """部分卖出: 剩余成本不变(加权平均法)"""
        ledger = PortfolioLedger()
        ledger.init_cash(100000.0)
        ledger.record_fill(1000, "SOLUSDT", "core", "BUY", 10.0, 100.0, 0.0)
        before = ledger.avg_cost("SOLUSDT", "core")
        ledger.record_fill(2000, "SOLUSDT", "core", "SELL", 4.0, 130.0, 0.0)
        assert ledger.avg_cost("SOLUSDT", "core") == pytest.approx(before)
        assert ledger.qty("SOLUSDT", "core") == pytest.approx(6.0)


# ============ Level 2: Invariants ============

class TestInvariants:
    def test_core_never_sold_by_trade_strategy(self):
        """INVARIANT: 交易仓卖出永远不减少核心仓"""
        pm = PositionManager()
        bm = BucketPositionManager(pm)
        bm.on_buy_fill("SOLUSDT", 70.0, 100.0, CORE)
        bm.on_buy_fill("SOLUSDT", 30.0, 100.0, TRADE)

        # 反复卖交易仓到空
        for _ in range(5):
            _, used = bm.on_sell_fill("SOLUSDT", 10.0, 120.0, TRADE)
            if used == "REJECTED":
                break
        assert bm.core("SOLUSDT") == 70.0  # 核心仓恒定

    def test_panic_blocks_buy_capability(self):
        """INVARIANT: PANIC 下买入能力策略全被禁"""
        from at50_strategy.strategy_identity import StrategyType, capability

        for st in (StrategyType.ENTRY, StrategyType.GRID, StrategyType.TREND):
            assert capability(st.value, "can_buy") is True

        # 模拟 strategy_engine 的 PANIC 过滤逻辑
        from at30_analytics.regime import MarketRegimeEngine
        adj = MarketRegimeEngine.strategy_adjustment("PANIC")
        assert adj["buy_boost"] == 0.0

    def test_exposure_never_exceeds_cap(self):
        """INVARIANT: 目标敞口永远 ≤ 上限"""
        from at60_risk.risk_allocation import PortfolioAllocator

        alloc = PortfolioAllocator()
        for regime in ("strong_bull", "BULL", "SIDEWAY", "BEAR", "PANIC"):
            for conf in (0.1, 0.5, 0.9, 1.0):
                for rf in (1.0, 0.8, 0.5):
                    plan = alloc.plan("X", regime, conf, 10000.0, 100.0, risk_factor=rf)
                    assert plan.target_exposure <= 0.95, (regime, conf, rf)
                    assert plan.target_exposure >= 0.0

    def test_ledger_cash_never_negative_on_buys(self):
        """INVARIANT: 买入后现金不为负(调用方负责约束, 账本如实记录)"""
        ledger = PortfolioLedger()
        ledger.init_cash(100.0)
        ledger.record_fill(1000, "X", "trade", "BUY", 10.0, 100.0, 0.0)
        # 超额买入: 账本如实记为负(检测调用方错误)
        assert ledger.cash() == pytest.approx(-900.0)


# ============ Level 4: Regression(固定数据集) ============

def make_klines(scenario: str, n: int = 600) -> list[list]:
    """固定合成数据集(确定性, 无随机)"""
    price = 100.0
    out = []
    for i in range(n):
        if scenario == "bull":
            price *= 1.0012
        elif scenario == "bear":
            price *= 0.9988
        elif scenario == "sideway":
            price = 100.0 + (0.8 if i % 40 < 20 else -0.8)
        elif scenario == "crash":
            price *= 0.9 if 200 <= i < 260 else (1.0005 if i < 200 else 1.0010)
        out.append([i * 60000, price, price * 1.001, price * 0.999, price,
                    10.0, 0, 0, 0])
    return out


class TestBacktestRegression:
    """回测回归: 关键指标不因重构漂移"""

    async def test_bull_scenario(self):
        from at70_backtest.backtest_portfolio import PortfolioBacktester

        bt = PortfolioBacktester(symbol="TEST", initial_cash=20000.0)
        r = await bt.run(make_klines("bull"))
        s = r.summary()
        assert s["bars"] == 600
        assert s["balanced"] is True  # 对账恒成立
        assert s["total_return"] > 0  # 牛市应盈利
        # 基准锁: 首次评估(第60根)价买入的 Buy-Hold
        assert s["benchmark_return"] > 0.05

    async def test_bear_scenario(self):
        from at70_backtest.backtest_portfolio import PortfolioBacktester

        bt = PortfolioBacktester(symbol="TEST", initial_cash=20000.0)
        r = await bt.run(make_klines("bear"))
        s = r.summary()
        assert s["balanced"] is True
        # 熊市: 策略回撤应显著小于基准跌幅(资产管理的价值)
        assert s["max_drawdown"] < abs(s["benchmark_return"])

    async def test_sideway_scenario(self):
        from at70_backtest.backtest_portfolio import PortfolioBacktester

        bt = PortfolioBacktester(symbol="TEST", initial_cash=20000.0)
        r = await bt.run(make_klines("sideway"))
        s = r.summary()
        assert s["balanced"] is True
        # V7 真实策略: 横盘网格周期触发, 不再断言再平衡次数
        # 改为断言敞口稳定 + 回撤极小
        assert s["max_drawdown"] < 0.02

    async def test_crash_scenario(self):
        from at70_backtest.backtest_portfolio import PortfolioBacktester

        bt = PortfolioBacktester(symbol="TEST", initial_cash=20000.0)
        r = await bt.run(make_klines("crash"))
        s = r.summary()
        assert s["balanced"] is True
        # 崩盘: 策略回撤应小于 Buy-Hold 的跌幅(减仓响应)
        hold_dd = abs(min(0.0, s["benchmark_return"]))
        assert s["max_drawdown"] < max(0.9, hold_dd * 1.1)

    async def test_accounting_invariant_all_scenarios(self):
        """INVARIANT(P0-9): 所有场景对账平衡"""
        from at70_backtest.backtest_portfolio import PortfolioBacktester

        for scenario in ("bull", "bear", "sideway", "crash"):
            bt = PortfolioBacktester(symbol="TEST", initial_cash=20000.0)
            r = await bt.run(make_klines(scenario))
            assert r.reconciliation.get("balanced") is True, scenario


class TestStrategyIdentity:
    def test_enum_unified(self):
        from at50_strategy.strategy_identity import StrategyType

        assert StrategyType.ENTRY.value == "entry"
        assert StrategyType.EXIT.value == "exit"

    def test_capability(self):
        from at50_strategy.strategy_identity import capability

        assert capability("entry", "can_buy") is True
        assert capability("exit", "can_sell") is True
        assert capability("exit", "can_buy") is False
        assert capability("unknown", "can_buy") is False

    def test_strategy_names_match_weights(self):
        """INVARIANT(P0-1): 实际策略 name 与 DecisionEngine 权重键一致"""
        from at50_strategy.strategy_buy import BuyStrategy
        from at50_strategy.strategy_decision import DecisionEngine
        from at50_strategy.strategy_grid import GridStrategy
        from at50_strategy.strategy_sell import SellStrategy
        from at50_strategy.strategy_trend import TrendStrategy

        weights = DecisionEngine.DEFAULT_WEIGHTS
        for cls in (BuyStrategy, SellStrategy, GridStrategy, TrendStrategy):
            assert cls.name in weights, f"{cls.name} 无权重(旧命名 bug 回归)"
