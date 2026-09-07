"""V9.0 M2.4: at80_optimizer 测试

验证: 候选生成 / 目标函数 / settings 覆盖与恢复 / 排名提案 / 落库(不自动 activate)。
"""

import pytest

from at01_common.settings import get_settings
from at80_optimizer.optimizer import ParamOptimizer
from at80_optimizer.report import render_report


class TestCandidateGeneration:
    def test_cartesian_product(self):
        opt = ParamOptimizer()
        cands = opt.candidate_params({"a": 1}, {"x": [1, 2], "y": [10, 20]})
        assert len(cands) == 4
        assert {"a": 1, "x": 1, "y": 10} in cands
        assert {"a": 1, "x": 2, "y": 20} in cands

    def test_empty_grid_returns_base(self):
        opt = ParamOptimizer()
        assert opt.candidate_params({"a": 1}, {}) == [{"a": 1}]


class TestObjective:
    def test_penalizes_drawdown(self):
        opt = ParamOptimizer(drawdown_penalty=0.5)
        score = opt.objective({"total_return": 0.2, "max_drawdown": 0.1})
        assert score == pytest.approx(0.2 - 0.5 * 0.1)


class TestSettingsOverride:
    def test_apply_and_restore(self):
        opt = ParamOptimizer()
        s = get_settings()
        orig = s.grid_count
        opt._apply_params({"grid_count": 999})
        assert get_settings().grid_count == 999
        opt.restore_settings()
        assert get_settings().grid_count == orig

    def test_ignore_unknown_keys(self):
        opt = ParamOptimizer()
        opt._apply_params({"nonexistent_param": 1})
        assert "nonexistent_param" not in opt._touched
        assert opt._touched == {}


class TestOptimizeFlow:
    async def test_ranked_proposals_no_persist(self):
        opt = ParamOptimizer()

        async def fake_eval(params, klines, btc_klines=None):
            # 越接近 5 分越高
            return {
                "total_return": 0.1, "max_drawdown": 0.05,
                "sharpe": 1.0, "win_rate": 0.5, "profit_factor": 1.5,
                "_score": -abs(params.get("grid_count", 0) - 5),
            }

        proposals = await opt.optimize(
            {"a": 1}, {"grid_count": [3, 5, 7]}, [], persist=False, evaluator=fake_eval
        )
        assert len(proposals) == 3
        # 降序: grid_count=5 最优(版本名绑定生成序号, 非排名)
        assert proposals[0]["params"]["grid_count"] == 5
        assert proposals[0]["version"] == "opt-001"
        assert proposals[0]["score"] >= proposals[1]["score"] >= proposals[2]["score"]
        # 不自动 activate(提案阶段无从谈起, 但确认结构)
        assert "score" in proposals[0] and "metrics" in proposals[0]

    async def test_optimize_persists_versions(self, db_tables):
        opt = ParamOptimizer()

        async def fake_eval(params, klines, btc_klines=None):
            return {"total_return": 0.1, "max_drawdown": 0.05, "_score": float(params.get("grid_count", 0))}

        proposals = await opt.optimize(
            {}, {"grid_count": [3, 4]}, [], persist=True, evaluator=fake_eval
        )
        assert len(proposals) == 2

        from at50_strategy.strategy_version import StrategyVersionManager

        rows = await StrategyVersionManager().list()
        versions = {r["version"] for r in rows}
        assert "opt-000" in versions and "opt-001" in versions
        assert all(r["active"] is False for r in rows)  # 不自动 activate
        assert any(r["backtest_result"] is not None for r in rows)


class TestReport:
    def test_render_ranked(self):
        proposals = [
            {"version": "opt-000", "score": 0.15,
             "metrics": {"total_return": 0.2, "max_drawdown": 0.1, "sharpe": 1.5,
                         "win_rate": 0.6, "profit_factor": 2.0}},
            {"version": "opt-001", "score": 0.05,
             "metrics": {"total_return": 0.1, "max_drawdown": 0.1, "sharpe": 0.5,
                         "win_rate": 0.4, "profit_factor": 1.0}},
        ]
        md = render_report(proposals)
        assert "策略优化提案" in md
        assert "opt-000" in md and "opt-001" in md
        assert md.index("opt-000") < md.index("opt-001")

    def test_render_empty(self):
        assert "无候选" in render_report([])
