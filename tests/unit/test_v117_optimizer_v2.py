"""V11.1 P1-2 Optimizer V2 防过拟合单元测试。

覆盖: 网格展开 / 过拟合间隙 / 夏普 / 风险调整得分 / build_param_result 派生 /
rank_params 排序 / OptimizerV2 编排容错。
"""

import pytest

from at70_backtest.backtest_optimizer import (
    OptimizerV2,
    build_grid,
    build_param_result,
    compute_overfit_gap,
    compute_sharpe,
    rank_params,
    risk_adjusted_score,
)


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------

def test_build_grid_cartesian_product():
    g = build_grid({"a": [1.0, 2.0], "b": [10.0, 20.0, 30.0]})
    assert len(g) == 6
    assert {"a": 1.0, "b": 30.0} in g
    assert {"a": 2.0, "b": 20.0} in g


def test_build_grid_empty_spec():
    assert build_grid({}) == [{}]


def test_compute_overfit_gap():
    # 训练收益高、验证收益低 → 正间隙(过拟合)
    assert compute_overfit_gap([0.30, 0.30], [0.10, 0.10]) == pytest.approx(0.20)
    # 验证优于训练 → 负间隙(无过拟合)
    assert compute_overfit_gap([0.05], [0.15]) == pytest.approx(-0.10)


def test_compute_overfit_gap_empty():
    assert compute_overfit_gap([], [0.1]) == 0.0
    assert compute_overfit_gap([0.1], []) == 0.0


def test_compute_sharpe():
    assert compute_sharpe([0.10, 0.10, 0.10]) == 0.0  # 零波动
    assert compute_sharpe([0.10, 0.30]) > 0
    assert compute_sharpe([]) == 0.0


def test_risk_adjusted_score_no_overfit():
    # 无过拟合(gap=0)、零均值 → 得分 = 鲁棒性
    assert risk_adjusted_score(80.0, 0.0, 0.0) == pytest.approx(80.0)


def test_risk_adjusted_score_magnitude_term():
    # 等鲁棒性下, 更高平均收益 → 更高得分(打破尺度不变)
    assert risk_adjusted_score(80.0, 0.10, 0.0) > risk_adjusted_score(80.0, 0.0, 0.0)


def test_risk_adjusted_score_overfit_halves():
    # gap >= 10% → 得分减半
    assert risk_adjusted_score(80.0, 0.0, 0.10) == pytest.approx(40.0)
    assert risk_adjusted_score(80.0, 0.0, 0.50) == pytest.approx(40.0)  # 封顶 0.5


# ---------------------------------------------------------------------------
# build_param_result 派生字段
# ---------------------------------------------------------------------------

def test_build_param_result_derives_fields():
    r = build_param_result(
        {"threshold": 80.0},
        train_returns=[0.30, 0.30],
        test_returns=[0.10, 0.20, 0.30],
    )
    assert r.mean_test_return == pytest.approx(0.20)
    assert r.worst_test_return == pytest.approx(0.10)
    assert r.profitable_ratio == 1.0
    assert r.overfit_gap == pytest.approx(0.10)
    assert r.robustness > 0
    assert r.risk_adjusted_score == pytest.approx(
        risk_adjusted_score(r.robustness, r.mean_test_return, 0.10)
    )


def test_build_param_result_empty_test():
    r = build_param_result({"x": 1.0}, [0.1], [])
    assert r.mean_test_return == 0.0
    assert r.rank == 0
    assert r.robustness == 0.0


# ---------------------------------------------------------------------------
# rank_params 排序
# ---------------------------------------------------------------------------

def test_rank_params_sorts_by_risk_adjusted_desc():
    a = build_param_result({"t": 1.0}, [0.10, 0.10], [0.20, 0.20])  # 高鲁棒低过拟合
    b = build_param_result({"t": 2.0}, [0.50, 0.50], [0.20, 0.20])  # 同验证但高过拟合
    c = build_param_result({"t": 3.0}, [0.05, 0.05], [-0.30, -0.30])  # 全亏
    ranked = rank_params([c, a, b])
    assert ranked[0].params == {"t": 1.0}
    assert ranked[0].rank == 1
    assert ranked[-1].params == {"t": 3.0}
    assert [r.rank for r in ranked] == [1, 2, 3]


def test_rank_params_does_not_mutate_input():
    a = build_param_result({"t": 1.0}, [0.1], [0.2])
    b = build_param_result({"t": 2.0}, [0.1], [0.1])
    orig = [a, b]
    rank_params(orig)
    assert orig[0].params == {"t": 1.0}  # 顺序未变


def test_rank_params_prefers_low_overfit_over_high_return():
    # 参数 A: 验证收益一般但无过拟合; 参数 B: 验证收益高但严重过拟合
    a = build_param_result({"t": "A"}, [0.12, 0.12], [0.10, 0.10])
    b = build_param_result({"t": "B"}, [0.50, 0.50], [0.15, 0.15])
    ranked = rank_params([b, a])
    assert ranked[0].params == {"t": "A"}  # 防过拟合: A 排序更前


# ---------------------------------------------------------------------------
# OptimizerV2 编排
# ---------------------------------------------------------------------------

async def test_optimizer_ranks_and_skips_failure():
    async def fake_evaluate(params):
        t = params["t"]
        if t == 999:
            raise RuntimeError("boom")
        return ([t / 100, t / 100], [t / 100, (t + 1) / 100])

    opt = OptimizerV2([{"t": 1.0}, {"t": 999}, {"t": 2.0}], fake_evaluate)
    ranked = await opt.optimize()
    assert len(ranked) == 3
    assert ranked[0].params == {"t": 2.0}  # 更高验证收益排前
    # 失败点: 空结果排在最后, 不阻断
    assert ranked[-1].params == {"t": 999}
    assert ranked[-1].rank == 3


async def test_optimizer_empty_grid():
    opt = OptimizerV2([], lambda p: None)
    assert await opt.optimize() == []
