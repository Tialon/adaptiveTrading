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


class StrategyVersionManager(LoggerMixin):
    """策略版本快照管理器"""

    # 纳入快照的参数(策略 + 组合三桶)
    TRACKED_PARAMS = (
        "strategy_enabled", "grid_upper_pct", "grid_lower_pct", "grid_count",
        "trend_fast_period", "trend_slow_period", "buy_dip_pct",
        "sell_profit_pct", "sell_trailing_drawdown",
        "entry_buy_threshold", "entry_observe_threshold",
        "risk_max_position_pct", "risk_max_single_order_pct",
        "portfolio_core_ratio", "portfolio_trading_ratio", "portfolio_cash_ratio",
    )

    def snapshot_params(self, settings: Optional[Any] = None) -> dict[str, Any]:
        """从 settings 提取参数快照"""
        s = settings or get_settings()
        return {name: getattr(s, name, None) for name in self.TRACKED_PARAMS}

    async def snapshot(self, version: str, note: str = "", params: Optional[dict[str, Any]] = None) -> int | None:
        """快照当前参数为新版本(版本已存在则跳过, 返回已有 id)"""
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
