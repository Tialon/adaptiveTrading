"""V9.0 M2.3: 回测指标(win_rate/profit_factor/holding/sortino/calmar/attribution)测试

验证:
- LedgerEntry / record_fill 透传 strategy(归因口径)
- 闭环成交统计纯函数数值正确性
- Sortino(下行偏差)/ Calmar(total_return/max_drawdown)公式
- PortfolioBacktestResult.summary() 含 6 项新指标
"""

import math

import pytest

from at60_risk.risk_ledger import PortfolioLedger
from at70_backtest.backtest_portfolio import (
    PortfolioBacktestResult,
    compute_calmar,
    compute_closed_trade_metrics,
    compute_sortino,
)


class TestLedgerStrategyAttribution:
    def test_record_fill_passes_strategy(self):
        ledger = PortfolioLedger()
        ledger.init_cash(10000.0)
        buy = ledger.record_fill(1000, "SOLUSDT", "trade", "BUY", 10.0, 100.0, 1.0, strategy="trend_swing")
        assert buy.strategy == "trend_swing"
        assert buy.to_dict()["strategy"] == "trend_swing"
        sell = ledger.record_fill(2000, "SOLUSDT", "trade", "SELL", 10.0, 110.0, 1.1, strategy="exit_manager")
        assert sell.strategy == "exit_manager"

    def test_record_fill_default_strategy_empty(self):
        ledger = PortfolioLedger()
        ledger.init_cash(10000.0)
        entry = ledger.record_fill(1000, "SOLUSDT", "trade", "BUY", 10.0, 100.0, 1.0)
        assert entry.strategy == ""


class TestClosedTradeMetrics:
    def test_empty(self):
        m = compute_closed_trade_metrics([])
        assert m["win_rate"] == 0.0
        assert m["profit_factor"] == 0.0
        assert m["avg_holding_seconds"] == 0.0
        assert m["closed_trades"] == 0
        assert m["attribution"] == {}

    def test_mixed_trades(self):
        trades = [
            {"strategy": "trend_swing", "realized_pnl": 10.0, "holding_seconds": 100.0},
            {"strategy": "exit_manager", "realized_pnl": -5.0, "holding_seconds": 200.0},
        ]
        m = compute_closed_trade_metrics(trades)
        assert m["win_rate"] == pytest.approx(0.5)
        assert m["profit_factor"] == pytest.approx(2.0)  # 10 / 5
        assert m["avg_holding_seconds"] == pytest.approx(150.0)
        assert m["closed_trades"] == 2
        assert m["attribution"] == {"trend_swing": 10.0, "exit_manager": -5.0}

    def test_all_wins_profit_factor_inf(self):
        trades = [
            {"strategy": "a", "realized_pnl": 5.0, "holding_seconds": 1.0},
            {"strategy": "b", "realized_pnl": 3.0, "holding_seconds": 1.0},
        ]
        m = compute_closed_trade_metrics(trades)
        assert m["win_rate"] == pytest.approx(1.0)
        assert math.isinf(m["profit_factor"])

    def test_all_losses_profit_factor_zero(self):
        trades = [
            {"strategy": "a", "realized_pnl": -5.0, "holding_seconds": 1.0},
            {"strategy": "b", "realized_pnl": -3.0, "holding_seconds": 1.0},
        ]
        m = compute_closed_trade_metrics(trades)
        assert m["win_rate"] == pytest.approx(0.0)
        assert m["profit_factor"] == 0.0

    def test_unknown_strategy_grouped(self):
        trades = [
            {"strategy": "", "realized_pnl": 7.0, "holding_seconds": 1.0},
        ]
        m = compute_closed_trade_metrics(trades)
        assert m["attribution"] == {"unknown": 7.0}


class TestSortinoCalmar:
    def test_sortino_no_downside(self):
        # 全正收益 -> 无下行偏差 -> 0
        assert compute_sortino([0.01, 0.02, 0.015], bars_per_year_val=1.0) == 0.0

    def test_sortino_downside_only(self):
        # mean = 0.006667, downside = [-0.01], dd_std = 0.01
        sortino = compute_sortino([0.01, -0.01, 0.02], bars_per_year_val=1.0)
        assert sortino == pytest.approx(0.006667 / 0.01, rel=1e-3)

    def test_sortino_annualized(self):
        # 年化因子: sqrt(bars_per_year)
        bpy = 1440.0
        sortino = compute_sortino([0.01, -0.01, 0.02], bars_per_year_val=bpy)
        assert sortino == pytest.approx((0.006667 / 0.01) * math.sqrt(bpy), rel=1e-3)

    def test_sortino_empty(self):
        assert compute_sortino([], bars_per_year_val=1.0) == 0.0

    def test_calmar_basic(self):
        assert compute_calmar(0.20, 0.10) == pytest.approx(2.0)

    def test_calmar_zero_drawdown(self):
        assert compute_calmar(0.20, 0.0) == 0.0


class TestResultSummary:
    def test_summary_includes_new_metrics(self):
        r = PortfolioBacktestResult(symbol="SOLUSDT")
        s = r.summary()
        for key in ("win_rate", "profit_factor", "avg_holding_seconds",
                    "closed_trades", "sortino", "calmar", "attribution"):
            assert key in s, f"summary 缺少 {key}"
