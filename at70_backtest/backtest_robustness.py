"""Backtest V2 鲁棒性矩阵(V11.1 P1-1)

目标: 用「多维度矩阵 + 鲁棒性评分」取代「单一收益」作为策略是否可上线的判据。

矩阵四维(全组合 5×6×5×5 = 750 格):
- 时间窗口: 7 / 30 / 90 / 180 / 365 天。
- 市场态: BULL / NORMAL / SIDEWAY / VOLATILE / BEAR / PANIC(由窗口数据分类, 非强制)。
- 参数扰动: baseline / ±5% / ±10%(缩放 rebalance_tolerance, 测参数敏感性)。
- 执行成本: 0 / 5 / 10 / 20 / 30 bps 滑点(测对价差/滑点的脆弱性)。

鲁棒性评分(0~100)不奖励「单次高收益」, 而是奖励「多数格子盈利 + 最坏格子不至于亏损
+ 跨格子收益稳定」:
    score = 100 × 盈利占比 × (0.5×最坏稳健度 + 0.5×稳定性)
- 盈利占比 profitable_ratio: 盈利格 / 总格(全亏 → 0 分, 直接判「不可用」)。
- 最坏稳健度 worst_component: clamp(1 + 最坏收益, 0, 1)(最坏 -100% → 0, ≥0 → 1)。
- 稳定性 stability: clamp(1 - 跨格收益标准差/0.10, 0, 1)(10% 标准差 → 0)。

纯函数 `compute_robustness` + 编排器 `RobustnessMatrixRunner`(注入 run_cell, 便于测试/替换),
与真实回测 `PortfolioBacktester` 解耦。
"""

import statistics
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from at01_common.logger import LoggerMixin

# ---- 矩阵维度常量 ----
TIME_WINDOWS_DAYS = (7, 30, 90, 180, 365)
REGIMES = ("BULL", "NORMAL", "SIDEWAY", "VOLATILE", "BEAR", "PANIC")
PARAM_PERTURBATIONS = (0.90, 0.95, 1.00, 1.05, 1.10)  # baseline / ±5% / ±10%
COST_BPS = (0, 5, 10, 20, 30)


@dataclass(frozen=True)
class CellConfig:
    """矩阵一格的四维坐标。"""

    time_days: int
    regime: str
    param_pert: float
    cost_bps: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "time_days": self.time_days,
            "regime": self.regime,
            "param_pert": self.param_pert,
            "cost_bps": self.cost_bps,
        }


@dataclass
class CellResult:
    """一格回测结果(单格失败不阻断整矩阵)。"""

    config: CellConfig
    total_return: float = 0.0
    sharpe: float = 0.0
    max_drawdown: float = 0.0
    trades: int = 0
    win_rate: float = 0.0
    final_equity: float = 0.0
    error: str = ""  # 非空表示该格执行失败(计入 cells, 不计入收益统计)

    @property
    def ok(self) -> bool:
        return not self.error


def grid() -> list[CellConfig]:
    """枚举全组合矩阵(750 格)。"""
    return [
        CellConfig(time_days=t, regime=r, param_pert=p, cost_bps=c)
        for t in TIME_WINDOWS_DAYS
        for r in REGIMES
        for p in PARAM_PERTURBATIONS
        for c in COST_BPS
    ]


def classify_regime(price_change: float, volatility: float) -> str:
    """由窗口价格变化 + 波动率分类市场态(纯函数, 确定性)。

    price_change: 窗口期价格涨跌幅(如 +0.15 = +15%)。
    volatility: 窗口期高低振幅(如 0.30 = 30%)。
    """
    if price_change < -0.10 and volatility >= 0.30:
        return "PANIC"
    if price_change < -0.10:
        return "BEAR"
    if price_change > 0.10 and volatility >= 0.30:
        return "VOLATILE"
    if price_change > 0.10:
        return "BULL"
    if abs(price_change) <= 0.05 and volatility <= 0.10:
        return "SIDEWAY"
    if volatility >= 0.30:
        return "VOLATILE"
    return "NORMAL"


@dataclass
class RobustnessReport:
    """鲁棒性报告(聚合所有格子)。"""

    cells: int = 0
    profitable: int = 0
    profitable_ratio: float = 0.0
    mean_return: float = 0.0
    median_return: float = 0.0
    std_return: float = 0.0
    worst_return: float = 0.0
    best_return: float = 0.0
    mean_sharpe: float = 0.0
    mean_max_drawdown: float = 0.0
    robustness_score: float = 0.0
    verdict: str = "无数据"
    regime_breakdown: dict[str, dict[str, float]] = field(default_factory=dict)
    cost_sensitivity: list[dict[str, float]] = field(default_factory=list)
    param_sensitivity: list[dict[str, float]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cells": self.cells,
            "profitable": self.profitable,
            "profitable_ratio": round(self.profitable_ratio, 4),
            "mean_return": round(self.mean_return, 4),
            "median_return": round(self.median_return, 4),
            "std_return": round(self.std_return, 4),
            "worst_return": round(self.worst_return, 4),
            "best_return": round(self.best_return, 4),
            "mean_sharpe": round(self.mean_sharpe, 3),
            "mean_max_drawdown": round(self.mean_max_drawdown, 4),
            "robustness_score": round(self.robustness_score, 1),
            "verdict": self.verdict,
            "regime_breakdown": {
                k: {kk: round(vv, 4) for kk, vv in v.items()}
                for k, v in self.regime_breakdown.items()
            },
            "cost_sensitivity": [
                {kk: round(vv, 4) for kk, vv in c.items()} for c in self.cost_sensitivity
            ],
            "param_sensitivity": [
                {kk: round(vv, 4) for kk, vv in p.items()} for p in self.param_sensitivity
            ],
        }


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def robustness_score(returns: list[float]) -> float:
    """从一组收益计算鲁棒性评分(0-100, 与 compute_robustness 同口径)。

    空序列返回 0。供 Optimizer V2 等跨窗口鲁棒性复用。
    """
    if not returns:
        return 0.0
    profitable = sum(1 for r in returns if r > 0) / len(returns)
    worst_component = _clamp01(1.0 + min(returns))
    std = statistics.pstdev(returns) if len(returns) > 1 else 0.0
    stability = _clamp01(1.0 - std / 0.10)
    return 100.0 * profitable * (0.5 * worst_component + 0.5 * stability)


def compute_robustness(cells: list[CellResult]) -> RobustnessReport:
    """聚合格子结果为鲁棒性报告(纯函数)。

    失败格(error 非空)计入 `cells` 但不参与收益统计; 全失败时返回「无数据」。
    """
    report = RobustnessReport(cells=len(cells))
    returns = [c.total_return for c in cells if c.ok]
    if not returns:
        return report

    n = len(returns)
    report.profitable = sum(1 for r in returns if r > 0)
    report.profitable_ratio = report.profitable / n
    report.mean_return = sum(returns) / n
    report.median_return = statistics.median(returns)
    report.std_return = statistics.pstdev(returns)
    report.worst_return = min(returns)
    report.best_return = max(returns)
    report.mean_sharpe = sum(c.sharpe for c in cells if c.ok) / n
    report.mean_max_drawdown = sum(c.max_drawdown for c in cells if c.ok) / n

    report.robustness_score = robustness_score(returns)
    report.verdict = (
        "稳健" if report.robustness_score >= 70.0 else
        ("脆弱" if report.robustness_score >= 40.0 else "不可用")
    )

    # 按市场态分组
    for regime in REGIMES:
        rs = [c.total_return for c in cells if c.ok and c.config.regime == regime]
        if not rs:
            continue
        report.regime_breakdown[regime] = {
            "count": float(len(rs)),
            "mean_return": sum(rs) / len(rs),
            "profitable_ratio": sum(1 for r in rs if r > 0) / len(rs),
        }

    # 按执行成本分组(成本敏感性)
    for bps in COST_BPS:
        rs = [c.total_return for c in cells if c.ok and c.config.cost_bps == bps]
        if not rs:
            continue
        report.cost_sensitivity.append({
            "cost_bps": float(bps),
            "count": float(len(rs)),
            "mean_return": sum(rs) / len(rs),
            "profitable_ratio": sum(1 for r in rs if r > 0) / len(rs),
        })

    # 按参数扰动分组(参数敏感性)
    for p in PARAM_PERTURBATIONS:
        rs = [c.total_return for c in cells if c.ok and c.config.param_pert == p]
        if not rs:
            continue
        report.param_sensitivity.append({
            "param_pert": p,
            "count": float(len(rs)),
            "mean_return": sum(rs) / len(rs),
            "profitable_ratio": sum(1 for r in rs if r > 0) / len(rs),
        })

    return report


class RobustnessMatrixRunner(LoggerMixin):
    """鲁棒性矩阵编排器: 遍历格子 → run_cell → compute_robustness。

    run_cell 由调用方注入(可接真实 PortfolioBacktester 或测试桩), 单格异常不阻断整矩阵。
    """

    def __init__(
        self,
        run_cell: Callable[[CellConfig], Awaitable[CellResult]],
        cells: Optional[list[CellConfig]] = None,
    ):
        self.run_cell = run_cell
        self.cells = cells or grid()

    async def run(self) -> RobustnessReport:
        results: list[CellResult] = []
        for cfg in self.cells:
            try:
                results.append(await self.run_cell(cfg))
            except Exception as e:  # 单格失败不影响整矩阵
                self.logger.warning("矩阵单格执行失败", cell=cfg.to_dict(), error=str(e))
                results.append(CellResult(config=cfg, error=str(e)))
        report = compute_robustness(results)
        self.logger.info("鲁棒性矩阵完成", **report.to_dict())
        return report


async def run_portfolio_cell(
    cfg: CellConfig,
    klines_cache: dict[int, list[list[Any]]],
    symbol: str = "SOLUSDT",
    interval: str = "1m",
    base_rebalance_tolerance: float = 0.05,
) -> CellResult:
    """真实回测格: 按 (param_pert, cost_bps) 跑 PortfolioBacktester。

    - cost_bps → slippage_bps(执行成本)。
    - param_pert → rebalance_tolerance × param_pert(参数敏感性)。
    - regime 由窗口数据分类后回填(见 classify_regime), 不强制市场态。
    """
    from at70_backtest.backtest_portfolio import PortfolioBacktester

    klines = klines_cache.get(cfg.time_days)
    if klines is None:
        from at70_backtest.backtest_portfolio import fetch_klines_paged

        klines = await fetch_klines_paged(symbol, interval=interval, days=cfg.time_days)
        klines_cache[cfg.time_days] = klines

    if len(klines) < 100:
        return CellResult(config=cfg, error="K线不足")

    bt = PortfolioBacktester(
        symbol=symbol,
        interval=interval,
        slippage_bps=float(cfg.cost_bps),
        rebalance_tolerance=base_rebalance_tolerance * cfg.param_pert,
    )
    res = await bt.run(klines)
    if res.bars < 100:
        return CellResult(config=cfg, error="样本不足")

    # 回填观察到的市场态(而非强制)
    first = float(klines[0][1])
    last = float(klines[-1][4])
    hi = max(float(k[2]) for k in klines)
    lo = min(float(k[3]) for k in klines)
    price_change = (last - first) / first if first > 0 else 0.0
    volatility = (hi - lo) / ((hi + lo) / 2) if (hi + lo) > 0 else 0.0
    regime = classify_regime(price_change, volatility)

    return CellResult(
        config=CellConfig(cfg.time_days, regime, cfg.param_pert, cfg.cost_bps),
        total_return=res.total_return,
        sharpe=res.sharpe,
        max_drawdown=res.max_drawdown,
        trades=res.trade_count,
        win_rate=res.win_rate,
        final_equity=res.final_equity,
    )
