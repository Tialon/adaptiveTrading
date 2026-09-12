"""
Param Optimizer(V9.0 M2.4) — AI 优化器 / 实验管理

确定性网格搜索 -> 复用 V7 真实管线回测 -> 落库 strategy_versions -> 排序提案。

原则: AI 只优化不交易 —— 优化结果只"提案"(persist 后 active=False),
需人工 `StrategyVersionManager.activate(version)` 确认后才生效。
"""

import itertools
from typing import Any, Awaitable, Callable, Optional

from at01_common.logger import LoggerMixin
from at01_common.settings import get_settings

# 可注入的评估函数: (params, klines, btc_klines) -> summary dict
Evaluator = Callable[[dict[str, Any], list, Optional[list]], Awaitable[dict[str, Any]]]


class ParamOptimizer(LoggerMixin):
    """参数优化器"""

    def __init__(
        self,
        symbol: str = "SOLUSDT",
        initial_cash: float = 20000.0,
        interval: str = "1m",
        slippage_bps: float = 10.0,
        drawdown_penalty: float = 0.5,
    ):
        self.symbol = symbol
        self.initial_cash = initial_cash
        self.interval = interval
        self.slippage_bps = slippage_bps
        self.drawdown_penalty = drawdown_penalty
        self._touched: dict[str, Any] = {}  # 被覆盖的 settings 原值(供 restore)

    # ---------- 候选生成 ----------

    def candidate_params(
        self, base: dict[str, Any], grid: dict[str, list[Any]]
    ) -> list[dict[str, Any]]:
        """确定性格点搜索: base 打底, grid 各维取值做笛卡尔积"""
        keys = list(grid.keys())
        value_lists = [grid[k] for k in keys]
        out: list[dict[str, Any]] = []
        for combo in itertools.product(*value_lists):
            candidate = dict(base)
            for k, v in zip(keys, combo):
                candidate[k] = v
            out.append(candidate)
        return out

    # ---------- settings 覆盖(临时, 评估后 restore) ----------

    def _apply_params(self, params: dict[str, Any]) -> None:
        s = get_settings()
        for k, v in params.items():
            if hasattr(s, k):
                if k not in self._touched:
                    self._touched[k] = getattr(s, k)
                setattr(s, k, v)

    def restore_settings(self) -> None:
        s = get_settings()
        for k, v in self._touched.items():
            setattr(s, k, v)
        self._touched.clear()

    # ---------- 目标函数 ----------

    def objective(self, metrics: dict[str, Any]) -> float:
        """默认目标: 最大化 total_return, 惩罚 max_drawdown"""
        return metrics.get("total_return", 0.0) - self.drawdown_penalty * metrics.get("max_drawdown", 0.0)

    # ---------- 评估 ----------

    async def evaluate(
        self,
        params: dict[str, Any],
        klines: list,
        btc_klines: Optional[list] = None,
    ) -> dict[str, Any]:
        """回测单个候选, 返回 summary(附 _score)"""
        from at80_backtest.backtest_portfolio import PortfolioBacktester

        self._apply_params(params)
        bt = PortfolioBacktester(
            symbol=self.symbol,
            initial_cash=self.initial_cash,
            interval=self.interval,
            slippage_bps=self.slippage_bps,
        )
        result = await bt.run(klines, btc_klines=btc_klines)
        summary = result.summary()
        summary["_score"] = self.objective(summary)
        return summary

    # ---------- 持久化 ----------

    async def persist(
        self, version: str, params: dict[str, Any], metrics: dict[str, Any]
    ) -> bool:
        """落库一条 strategy_versions(active=False, 需人工确认)"""
        from at30_strategy.strategy_version import StrategyVersionManager

        manager = StrategyVersionManager()
        vid = await manager.snapshot(
            version, note="optimizer proposal", params=params, backtest_result=metrics
        )
        return vid is not None

    # ---------- 主编排 ----------

    async def optimize(
        self,
        base: dict[str, Any],
        grid: dict[str, list[Any]],
        klines: list,
        btc_klines: Optional[list] = None,
        tag: str = "opt",
        evaluator: Optional[Evaluator] = None,
        persist: bool = True,
    ) -> list[dict[str, Any]]:
        """生成候选 -> 逐一评估 -> 落库 -> 按得分降序返回提案

        evaluator 可注入(测试用); persist=False 时只提案不落库。
        """
        evaluator = evaluator or self.evaluate
        proposals: list[dict[str, Any]] = []
        try:
            for i, params in enumerate(self.candidate_params(base, grid)):
                summary = await evaluator(params, klines, btc_klines=btc_klines)
                version = f"{tag}-{i:03d}"
                if persist:
                    await self.persist(version, params, summary)
                proposals.append({
                    "version": version,
                    "params": params,
                    "score": summary.get("_score", 0.0),
                    "metrics": {k: v for k, v in summary.items() if k != "_score"},
                })
        finally:
            self.restore_settings()
        proposals.sort(key=lambda p: p["score"], reverse=True)
        return proposals
