"""V11.1 P1-1 Backtest V2 鲁棒性矩阵单元测试。

覆盖: 矩阵枚举 / 市场态分类 / 鲁棒性评分(纯函数)/ 编排器容错与聚合。
"""

import pytest

from at80_backtest.backtest_robustness import (
    COST_BPS,
    PARAM_PERTURBATIONS,
    REGIMES,
    TIME_WINDOWS_DAYS,
    CellConfig,
    CellResult,
    RobustnessMatrixRunner,
    classify_regime,
    compute_robustness,
    grid,
)


def _cell(return_, regime="NORMAL", cost=10, param=1.0, error="", **kw) -> CellResult:
    return CellResult(
        config=CellConfig(time_days=30, regime=regime, param_pert=param, cost_bps=cost),
        total_return=return_,
        sharpe=kw.get("sharpe", 0.0),
        max_drawdown=kw.get("max_drawdown", 0.0),
        error=error,
    )


# ---------------------------------------------------------------------------
# 矩阵枚举
# ---------------------------------------------------------------------------

def test_grid_full_cross_product():
    cells = grid()
    assert len(cells) == len(TIME_WINDOWS_DAYS) * len(REGIMES) * len(PARAM_PERTURBATIONS) * len(COST_BPS)
    assert len(cells) == 750
    # 唯一性: 四维坐标无重复
    assert len({(c.time_days, c.regime, c.param_pert, c.cost_bps) for c in cells}) == 750


def test_grid_contains_corner_cells():
    cells = grid()
    assert CellConfig(7, "BULL", 0.90, 0) in cells
    assert CellConfig(365, "PANIC", 1.10, 30) in cells
    assert CellConfig(90, "NORMAL", 1.00, 10) in cells


# ---------------------------------------------------------------------------
# 市场态分类
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "change,vol,expected",
    [
        (+0.20, 0.10, "BULL"),
        (-0.20, 0.10, "BEAR"),
        (-0.25, 0.40, "PANIC"),
        (+0.25, 0.40, "VOLATILE"),  # 大涨 + 高波动 → VOLATILE(非 BULL)
        (+0.02, 0.05, "SIDEWAY"),
        (+0.08, 0.15, "NORMAL"),
        (+0.00, 0.35, "VOLATILE"),  # 无方向 + 高波动
    ],
)
def test_classify_regime(change, vol, expected):
    assert classify_regime(change, vol) == expected


# ---------------------------------------------------------------------------
# compute_robustness(纯函数)
# ---------------------------------------------------------------------------

def test_empty_cells_no_data():
    r = compute_robustness([])
    assert r.cells == 0
    assert r.verdict == "无数据"
    assert r.robustness_score == 0.0


def test_all_failed_cells_no_data():
    r = compute_robustness([_cell(0.0, error="boom")])
    assert r.verdict == "无数据"
    assert r.robustness_score == 0.0


def test_all_profitable_robust():
    cells = [_cell(0.10 + 0.01 * i) for i in range(10)]
    r = compute_robustness(cells)
    assert r.profitable_ratio == 1.0
    assert r.worst_return > 0
    assert r.robustness_score >= 70.0
    assert r.verdict == "稳健"


def test_all_losing_unusable():
    cells = [_cell(-0.05 - 0.01 * i) for i in range(10)]
    r = compute_robustness(cells)
    assert r.profitable_ratio == 0.0
    assert r.worst_return < 0
    assert r.robustness_score < 40.0
    assert r.verdict == "不可用"


def test_single_great_among_many_bad_penalized():
    # 一格大赚, 九格大亏 → 鲁棒性必须低(不奖励单次高收益)
    cells = [_cell(0.50)] + [_cell(-0.30) for _ in range(9)]
    r = compute_robustness(cells)
    assert r.profitable_ratio == 0.1
    assert r.worst_return == -0.30
    assert r.robustness_score < 40.0


def test_median_robust_to_outlier():
    cells = [_cell(0.02) for _ in range(5)] + [_cell(10.0)]  # 一个极端值
    r = compute_robustness(cells)
    assert r.median_return == pytest.approx(0.02)
    assert r.mean_return > r.median_return  # 均值被极端值拉高


def test_regime_breakdown():
    cells = [
        _cell(0.10, regime="BULL"),
        _cell(0.05, regime="BULL"),
        _cell(-0.05, regime="BEAR"),
    ]
    r = compute_robustness(cells)
    assert r.regime_breakdown["BULL"]["count"] == 2
    assert r.regime_breakdown["BULL"]["mean_return"] == pytest.approx(0.075)
    assert r.regime_breakdown["BULL"]["profitable_ratio"] == 1.0
    assert r.regime_breakdown["BEAR"]["mean_return"] == pytest.approx(-0.05)
    assert "SIDEWAY" not in r.regime_breakdown  # 无数据的 regime 不出现


def test_cost_sensitivity_monotonic_degradation():
    # 成本越高收益越低(每档 2 格, 0bps 收益高 / 30bps 收益低)
    cells = []
    for bps in COST_BPS:
        ret = 0.20 - 0.006 * bps
        cells.append(_cell(ret, cost=bps))
        cells.append(_cell(ret - 0.01, cost=bps))
    r = compute_robustness(cells)
    means = {int(c["cost_bps"]): c["mean_return"] for c in r.cost_sensitivity}
    assert means[0] > means[30]  # 高成本降收益
    # 单调递减
    vals = [means[b] for b in sorted(means)]
    assert vals == sorted(vals, reverse=True)


def test_param_sensitivity_grouped():
    cells = [_cell(0.05, param=p) for p in (0.90, 1.10)]
    r = compute_robustness(cells)
    assert len(r.param_sensitivity) == 2
    assert {round(c["param_pert"], 2) for c in r.param_sensitivity} == {0.90, 1.10}


def test_to_dict_rounds_floats():
    r = compute_robustness([_cell(0.123456)])
    d = r.to_dict()
    assert d["mean_return"] == 0.1235
    assert isinstance(d["robustness_score"], float)


# ---------------------------------------------------------------------------
# RobustnessMatrixRunner 编排器
# ---------------------------------------------------------------------------

async def test_runner_aggregates_cells():
    async def fake(cfg: CellConfig) -> CellResult:
        return CellResult(config=cfg, total_return=0.10 + cfg.cost_bps / 1000.0)

    runner = RobustnessMatrixRunner(
        fake, cells=[CellConfig(30, "NORMAL", 1.0, b) for b in (0, 10, 30)]
    )
    report = await runner.run()
    assert report.cells == 3
    assert report.verdict == "稳健"


async def test_runner_skips_failing_cell():
    calls = []

    async def fake(cfg: CellConfig) -> CellResult:
        calls.append(cfg)
        if cfg.cost_bps == 10:
            raise RuntimeError("boom")
        return CellResult(config=cfg, total_return=0.10)

    runner = RobustnessMatrixRunner(
        fake, cells=[CellConfig(30, "NORMAL", 1.0, b) for b in (0, 10, 20)]
    )
    report = await runner.run()
    assert len(calls) == 3  # 异常格不阻断后续
    assert report.cells == 3
    # 只有 2 个成功格参与收益统计
    assert report.profitable == 2
    assert report.mean_return == pytest.approx(0.10)
