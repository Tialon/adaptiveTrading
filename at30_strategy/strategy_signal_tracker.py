"""
Signal Result Tracker(V3.0)

跟踪每个已落库信号的未来表现:
- 信号发出后, 每分钟用最新价更新 future_profit / max_profit / max_drawdown
- 窗口(默认 1 小时)结束后标记 final, 供 AI 学习与策略权重调整

数据流: run.py 周期任务 -> update(symbol, last_price) -> 批量落库
"""

import time
from typing import Any

from at01_common.logger import LoggerMixin


class SignalResultTracker(LoggerMixin):
    """信号结果跟踪器"""

    def __init__(self, window_seconds: int = 3600, update_interval: int = 60):
        self.window_seconds = window_seconds
        self.update_interval = update_interval
        # 内存跟踪表: signal_id -> record
        self._tracking: dict[int, dict[str, Any]] = {}
        self._dirty: bool = False

    # ---------- 注册 ----------

    def register(self, signal_id: int, symbol: str, strategy: str, side: str, entry_price: float) -> None:
        """信号落库后注册跟踪"""
        if signal_id is None or entry_price <= 0:
            return
        self._tracking[signal_id] = {
            "signal_id": signal_id,
            "symbol": symbol,
            "strategy": strategy,
            "side": side,
            "entry_price": entry_price,
            "future_profit": 0.0,
            "max_profit": 0.0,
            "max_drawdown": 0.0,
            "window_seconds": self.window_seconds,
            "final": False,
            "registered_at": time.time(),
            "_last_persist": 0.0,
        }

    async def load_open_from_db(self) -> int:
        """启动时从数据库加载未完成跟踪的信号"""
        from sqlalchemy import select

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import SignalResult

        try:
            async with AsyncSessionLocal() as session:
                rows = (
                    (
                        await session.execute(
                            select(SignalResult).where(SignalResult.final == False)  # noqa: E712
                        )
                    )
                    .scalars()
                    .all()
                )
                for r in rows:
                    self._tracking[r.signal_id] = {
                        "signal_id": r.signal_id,
                        "symbol": r.symbol,
                        "strategy": r.strategy,
                        "side": r.side,
                        "entry_price": r.entry_price,
                        "future_profit": r.future_profit,
                        "max_profit": r.max_profit,
                        "max_drawdown": r.max_drawdown,
                        "window_seconds": r.window_seconds,
                        "final": False,
                        "registered_at": time.time(),
                        "_last_persist": time.time(),
                    }
                return len(self._tracking)
        except Exception:
            self.logger.exception("加载未完成信号跟踪失败")
            return 0

    # ---------- 更新 ----------

    async def update(self, last_prices: dict[str, float]) -> int:
        """用最新价更新全部跟踪中的信号, 返回更新条数"""
        if not self._tracking:
            return 0
        now = time.time()
        updated = 0
        for rec in self._tracking.values():
            if rec["final"]:
                continue
            price = last_prices.get(rec["symbol"])
            if price is None or price <= 0:
                continue
            entry = rec["entry_price"]
            # 方向修正: SELL 信号看反方向收益
            raw_change = (price - entry) / entry
            profit = raw_change if rec["side"] == "BUY" else -raw_change
            rec["future_profit"] = profit
            rec["max_profit"] = max(rec["max_profit"], profit)
            rec["max_drawdown"] = min(rec["max_drawdown"], profit)
            # 窗口结束
            if now - rec["registered_at"] >= rec["window_seconds"]:
                rec["final"] = True
            updated += 1
        if updated:
            await self.persist()
        return updated

    # ---------- 持久化 ----------

    async def persist(self) -> None:
        """落库(upsert)"""
        from sqlalchemy import select

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import SignalResult

        try:
            async with AsyncSessionLocal() as session:
                for rec in self._tracking.values():
                    row = (
                        await session.execute(
                            select(SignalResult).where(SignalResult.signal_id == rec["signal_id"])
                        )
                    ).scalar_one_or_none()
                    if row is None:
                        row = SignalResult(
                            signal_id=rec["signal_id"],
                            symbol=rec["symbol"],
                            strategy=rec["strategy"],
                            side=rec["side"],
                            entry_price=rec["entry_price"],
                        )
                        session.add(row)
                    row.future_profit = rec["future_profit"]
                    row.max_profit = rec["max_profit"]
                    row.max_drawdown = rec["max_drawdown"]
                    row.window_seconds = rec["window_seconds"]
                    row.final = rec["final"]
                await session.commit()
            # 清理已完成且已落库的
            self._tracking = {k: v for k, v in self._tracking.items() if not v["final"]}
        except Exception:
            self.logger.exception("信号结果落库失败")

    # ---------- 统计(供 Decision Engine 权重调整 / AI) ----------

    def strategy_stats(self) -> dict[str, dict[str, float]]:
        """按策略聚合信号质量: 平均未来收益/胜率"""
        stats: dict[str, dict[str, float]] = {}
        buckets: dict[str, list[float]] = {}
        for rec in self._tracking.values():
            if not rec["final"]:
                continue
            buckets.setdefault(rec["strategy"], []).append(rec["future_profit"])
        # 内存中只有未 final 的, 统计主要靠数据库 -> load_open 之外需要全量查询
        return stats

    async def strategy_stats_from_db(self) -> dict[str, dict[str, float]]:
        """从数据库统计各策略信号质量(已 final 的)"""
        from sqlalchemy import select

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import SignalResult

        try:
            async with AsyncSessionLocal() as session:
                rows = (
                    (
                        await session.execute(
                            select(SignalResult).where(SignalResult.final == True)  # noqa: E712
                        )
                    )
                    .scalars()
                    .all()
                )
            buckets: dict[str, list[float]] = {}
            for r in rows:
                buckets.setdefault(r.strategy, []).append(r.future_profit)
            return {
                strat: {
                    "avg_future_profit": sum(profits) / len(profits),
                    "signal_count": len(profits),
                    "win_rate": sum(1 for p in profits if p > 0) / len(profits),
                }
                for strat, profits in buckets.items()
            }
        except Exception:
            return {}
