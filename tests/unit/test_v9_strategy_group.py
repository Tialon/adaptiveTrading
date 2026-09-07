"""V9.0 M2.2: 组合策略伞(薄分组层)测试

验证: group_of 映射、止盈阶梯 settings 化、版本快照按伞分组。
"""

import pytest

from at50_strategy.strategy_group import (
    PORTFOLIO_STRATEGIES,
    group_of,
    group_label,
)
from at50_strategy.strategy_sell import _parse_ladder, SellStrategy


class TestStrategyGroup:
    def test_group_mapping(self):
        assert group_of("trend") == "trend_swing"
        assert group_of("grid") == "mean_reversion"
        assert group_of("entry") == "mean_reversion"
        assert group_of("exit") == "exit_manager"

    def test_unknown_passthrough(self):
        # 融合后的 "decision" 不强行归类
        assert group_of("decision") == "decision"
        assert group_of(None) == "decision"
        assert group_of("") == "decision"

    def test_group_labels(self):
        assert group_label("trend") == "Trend Swing"
        assert group_label("grid") == "Mean Reversion"
        assert group_label("exit") == "Exit Manager"

    def test_three_portfolio_strategies(self):
        assert set(PORTFOLIO_STRATEGIES.keys()) == {
            "trend_swing", "mean_reversion", "exit_manager"
        }


class TestTakeProfitLadderSettings:
    def test_parse_default(self):
        ladder = _parse_ladder("5:20,10:30,20:50")
        assert ladder == [(0.05, 0.20), (0.10, 0.30), (0.20, 0.50)]

    def test_parse_unsorted(self):
        ladder = _parse_ladder("20:50,5:20,10:30")
        assert ladder[0][0] < ladder[1][0] < ladder[2][0]

    def test_parse_invalid_parts_skipped(self):
        ladder = _parse_ladder("5:20, bad, 10:30, , -1:10")
        assert ladder == [(0.05, 0.20), (0.10, 0.30)]

    def test_parse_empty_falls_back(self):
        s = SellStrategy(symbols=["SOLUSDT"])
        # 无 settings 覆盖时用默认阶梯
        assert s.take_profit_ladder == s.TAKE_PROFIT_LADDER
        assert s.take_profit_ladder[0] == (0.05, 0.20)


class TestVersionGrouping:
    def test_group_params_buckets(self):
        from at50_strategy.strategy_version import StrategyVersionManager

        m = StrategyVersionManager()
        params = {
            "trend_fast_period": 12,
            "trend_slow_period": 26,
            "grid_count": 10,
            "sell_trailing_drawdown": 0.05,
            "portfolio_core_ratio": 0.4,
            "strategy_enabled": "buy,sell",
        }
        grouped = m.group_params(params)
        assert grouped["trend_swing"]["trend_fast_period"] == 12
        assert grouped["mean_reversion"]["grid_count"] == 10
        assert grouped["exit_manager"]["sell_trailing_drawdown"] == 0.05
        assert grouped["portfolio"]["portfolio_core_ratio"] == 0.4
        assert grouped["global"]["strategy_enabled"] == "buy,sell"
