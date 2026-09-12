"""
Strategy Version(V9.0) — 策略参数版本快照

冻结当前策略/组合参数为不可变版本, 供回测-实盘对比与 AI 实验。

原则: AI 优化不是"直接改参数", 而是"生成新版本 -> 回测 -> 对比 -> 人工确认"。
本模块只负责版本快照与查询, 不自动应用参数。
"""

import json
from typing import Any, Optional

from at01_common.logger import LoggerMixin
from at01_common.settings import get_settings

# ---------------------------------------------------------------------------
# 版本备注词表(V13)
# ---------------------------------------------------------------------------
#
# 系统**自己**写的备注是机器标识(英文, 给程序看的); 人工写的备注才是给人看的。
# 展示层(配置向导 / AI 复盘)据此决定「原样展示」还是「换成中文解释」——
# 把 `startup baseline` 直接摊给用户看, 除了制造「这是什么意思」的疑问没有别的作用。
#
# 词表放在这里(而不是展示层): 它是**写入方**的词汇, 由写入方定义才不会两边漂移。
NOTE_STARTUP_BASELINE = "startup baseline"
NOTE_OPTIMIZER_PROPOSAL = "optimizer proposal"

AUTO_NOTES: frozenset[str] = frozenset({NOTE_STARTUP_BASELINE, NOTE_OPTIMIZER_PROPOSAL})


def is_auto_note(note: str | None) -> bool:
    """备注是否为系统自动写入的机器标识(而非人工填写)。"""
    return (note or "").strip().lower() in AUTO_NOTES


class StrategyVersionManager(LoggerMixin):
    """策略版本快照管理器"""

    # 纳入快照的参数(策略 + 组合三桶)
    TRACKED_PARAMS = (
        "strategy_enabled", "grid_upper_pct", "grid_lower_pct", "grid_count",
        "trend_fast_period", "trend_slow_period",
        "sell_trailing_drawdown", "sell_take_profit_ladder",
        "entry_buy_threshold", "entry_observe_threshold",
        "risk_max_position_pct", "risk_max_single_order_pct",
        "portfolio_core_ratio", "portfolio_trading_ratio", "portfolio_cash_ratio",
    )

    # V9.0: 参数 -> 组合策略伞(便于优化器按伞比较)
    PARAM_GROUPS: dict[str, str] = {
        "trend_fast_period": "trend_swing",
        "trend_slow_period": "trend_swing",
        "grid_upper_pct": "mean_reversion",
        "grid_lower_pct": "mean_reversion",
        "grid_count": "mean_reversion",
        "sell_trailing_drawdown": "exit_manager",
        "sell_take_profit_ladder": "exit_manager",
        "entry_buy_threshold": "entry",
        "entry_observe_threshold": "entry",
        "portfolio_core_ratio": "portfolio",
        "portfolio_trading_ratio": "portfolio",
        "portfolio_cash_ratio": "portfolio",
        "risk_max_position_pct": "portfolio",
        "risk_max_single_order_pct": "portfolio",
        "strategy_enabled": "global",
    }

    def snapshot_params(self, settings: Optional[Any] = None) -> dict[str, Any]:
        """从 settings 提取参数快照"""
        s = settings or get_settings()
        return {name: getattr(s, name, None) for name in self.TRACKED_PARAMS}

    def group_params(self, params: Optional[dict[str, Any]] = None) -> dict[str, dict[str, Any]]:
        """按组合策略伞分组参数快照 -> {group: {param: value}}"""
        params = params if params is not None else self.snapshot_params()
        grouped: dict[str, dict[str, Any]] = {}
        for name, value in params.items():
            group = self.PARAM_GROUPS.get(name, "global")
            grouped.setdefault(group, {})[name] = value
        return grouped

    async def snapshot(
        self,
        version: str,
        note: str = "",
        params: Optional[dict[str, Any]] = None,
        backtest_result: Optional[dict[str, Any]] = None,
    ) -> int | None:
        """快照当前参数为新版本(版本已存在则跳过, 返回已有 id)

        backtest_result(V9.0 M2.4): 优化器写入的回测结果 JSON(供实验台账)。
        """
        from sqlalchemy import select

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import StrategyVersion

        params = params if params is not None else self.snapshot_params()
        try:
            async with AsyncSessionLocal() as session:
                existing = (
                    await session.execute(
                        select(StrategyVersion).where(StrategyVersion.version == version)
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    return existing.id
                row = StrategyVersion(
                    version=version,
                    params=json.dumps(params, ensure_ascii=False),
                    note=note,
                    backtest_result=json.dumps(backtest_result, ensure_ascii=False) if backtest_result else None,
                    active=False,
                )
                session.add(row)
                await session.commit()
                self.logger.info("策略版本已快照", version=version)
                return row.id
        except Exception:
            self.logger.exception("策略版本快照失败", version=version)
            return None

    async def list(self, limit: int = 20) -> list[dict[str, Any]]:
        """版本列表"""
        from sqlalchemy import select

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import StrategyVersion

        try:
            async with AsyncSessionLocal() as session:
                rows = (
                    (
                        await session.execute(
                            select(StrategyVersion)
                            .order_by(StrategyVersion.id.desc())
                            .limit(limit)
                        )
                    )
                    .scalars()
                    .all()
                )
                return [
                    {
                        "id": r.id, "version": r.version, "note": r.note,
                        "params": json.loads(r.params) if r.params else {},
                        "backtest_result": json.loads(r.backtest_result) if r.backtest_result else None,
                        "active": r.active,
                        "created_at": r.created_at.isoformat() if r.created_at else "",
                    }
                    for r in rows
                ]
        except Exception:
            return []

    async def activate(self, version: str) -> bool:
        """标记某版本为 active(其余置 False)"""
        from sqlalchemy import select, update

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import StrategyVersion

        try:
            async with AsyncSessionLocal() as session:
                target = (
                    await session.execute(
                        select(StrategyVersion).where(StrategyVersion.version == version)
                    )
                ).scalar_one_or_none()
                if target is None:
                    return False
                await session.execute(
                    update(StrategyVersion).values(active=False)
                )
                target.active = True
                await session.commit()
                return True
        except Exception:
            self.logger.exception("策略版本激活失败", version=version)
            return False
