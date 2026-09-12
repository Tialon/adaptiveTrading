"""Optimizer V2(V11.1 P1-2)防过拟合参数优化。

流程: 网格搜索 → Walk-Forward(训练/验证滚动窗口)→ 鲁棒性(跨验证窗口)→ 风险调整排序。

核心思想: 不按「历史最高收益」选参数(会过拟合), 而是按「验证窗口鲁棒性 × 过拟合惩罚」排序:
- 鲁棒性 robustness: 复用 P1-1 的 `robustness_score`, 奖励「多数验证窗口盈利 + 最坏窗口
  不至于亏 + 跨窗口稳定」。
- 过拟合惩罚 overfit_gap: 训练平均收益 - 验证平均收益, 间隙越大越可能过拟合。

纯函数可独立测试; `OptimizerV2` 注入 evaluate 回调(返回某参数组在 walk-forward 下的
训练/验证收益序列), 便于接真实回测或测试桩。
"""

import statistics
from dataclasses import dataclass, field
from itertools import product
from typing import Any, Awaitable, Callable

from at01_common.logger import LoggerMixin
from at80_backtest.backtest_robustness import robustness_score


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


@dataclass
class ParamResult:
    """一组参数的 walk-forward 评估结果(派生字段由 build_param_result 计算)。"""

    params: dict[str, float]
    train_returns: list[float] = field(default_factory=list)
    test_returns: list[float] = field(default_factory=list)
    # 派生
    mean_test_return: float = 0.0
    worst_test_return: float = 0.0
    std_test_return: float = 0.0
    profitable_ratio: float = 0.0
    sharpe: float = 0.0
    overfit_gap: float = 0.0
    robustness: float = 0.0
    risk_adjusted_score: float = 0.0
    rank: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "params": self.params,
            "windows": len(self.test_returns),
            "mean_test_return": round(self.mean_test_return, 4),
            "worst_test_return": round(self.worst_test_return, 4),
            "std_test_return": round(self.std_test_return, 4),
            "profitable_ratio": round(self.profitable_ratio, 4),
            "sharpe": round(self.sharpe, 3),
            "overfit_gap": round(self.overfit_gap, 4),
            "robustness": round(self.robustness, 1),
            "risk_adjusted_score": round(self.risk_adjusted_score, 1),
            "rank": self.rank,
        }


def compute_overfit_gap(train_returns: list[float], test_returns: list[float]) -> float:
    """过拟合间隙 = 训练平均收益 - 验证平均收益(>0 越大越可能过拟合)。"""
    if not train_returns or not test_returns:
        return 0.0
    mean_train = sum(train_returns) / len(train_returns)
    mean_test = sum(test_returns) / len(test_returns)
    return mean_train - mean_test


def compute_sharpe(returns: list[float]) -> float:
    """简单夏普(验证窗口收益均值 / 标准差), 空或零波动返回 0。"""
    if not returns or len(returns) < 2:
        return 0.0
    std = statistics.pstdev(returns)
    if std <= 0:
        return 0.0
    return (sum(returns) / len(returns)) / std


def risk_adjusted_score(robustness: float, mean_return: float, overfit_gap: float) -> float:
    """风险调整得分 = 鲁棒性 × 收益量级项 × (1 - 0.5×过拟合惩罚)。

    - 收益量级项 magnitude = max(0, 1 + 平均验证收益): 打破纯鲁棒性的尺度不变
      (等形状但 10 倍收益不应并列), 又不让原始收益主导排序。
    - 过拟合惩罚 = clamp(overfit_gap / 0.10, 0, 1): 训练/验证间隙 10% → 得分减半。
    """
    overfit_penalty = _clamp01(overfit_gap / 0.10)
    magnitude = max(0.0, 1.0 + mean_return)
    return robustness * magnitude * (1.0 - 0.5 * overfit_penalty)


def build_param_result(
    params: dict[str, float],
    train_returns: list[float],
    test_returns: list[float],
) -> ParamResult:
    """纯函数: 从原始收益序列计算全部派生字段。空验证序列 → 全零(rank 未定)。"""
    r = ParamResult(params=params, train_returns=list(train_returns), test_returns=list(test_returns))
    if not test_returns:
        return r
    n = len(test_returns)
    r.mean_test_return = sum(test_returns) / n
    r.worst_test_return = min(test_returns)
    r.std_test_return = statistics.pstdev(test_returns) if n > 1 else 0.0
    r.profitable_ratio = sum(1 for x in test_returns if x > 0) / n
    r.sharpe = compute_sharpe(test_returns)
    r.overfit_gap = compute_overfit_gap(train_returns, test_returns)
    r.robustness = robustness_score(test_returns)
    r.risk_adjusted_score = risk_adjusted_score(r.robustness, r.mean_test_return, r.overfit_gap)
    return r


def rank_params(results: list[ParamResult]) -> list[ParamResult]:
    """纯函数: 按风险调整得分降序排序并赋 rank(1 起, 同分同序)。返回新列表(不改原列表)。"""
    ordered = sorted(results, key=lambda r: r.risk_adjusted_score, reverse=True)
    for i, r in enumerate(ordered, start=1):
        r.rank = i
    return ordered


def build_grid(param_spec: dict[str, list[float]]) -> list[dict[str, float]]:
    """网格搜索: 参数名 → 取值列表, 展开为笛卡尔积参数点列表。"""
    if not param_spec:
        return [{}]
    names = list(param_spec.keys())
    return [dict(zip(names, combo)) for combo in product(*(param_spec[n] for n in names))]


class OptimizerV2(LoggerMixin):
    """参数优化器: 网格搜索 → walk-forward 评估 → 风险调整排序。"""

    def __init__(
        self,
        grid: list[dict[str, float]],
        evaluate: Callable[[dict[str, float]], Awaitable[tuple[list[float], list[float]]]],
    ):
        self.grid = grid
        self.evaluate = evaluate

    async def optimize(self) -> list[ParamResult]:
        """遍历参数网格, 逐个评估并排序。单点异常不阻断(记为空结果)。"""
        results: list[ParamResult] = []
        for params in self.grid:
            try:
                train_returns, test_returns = await self.evaluate(params)
            except Exception as e:  # 单点失败不阻断
                self.logger.warning("参数点评估失败", params=params, error=str(e))
                train_returns, test_returns = [], []
            results.append(build_param_result(params, train_returns, test_returns))
        ranked = rank_params(results)
        if ranked:
            self.logger.info("参数优化完成", best=ranked[0].to_dict(), points=len(ranked))
        return ranked
