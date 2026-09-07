"""
Trading Journal(V9.0) — 成交结果日志

SELL 成交时记录一次完整闭环(开仓 -> 平仓), 沉淀为 AI 可复盘的"交易记忆":

    entry/exit 价格与时间、持仓时长、已实现盈亏、最大浮盈、最大回撤、失败原因。

记录时机: ExecutionEngine 在卖出成交后调用 record()(on_trade_record 回调)。
近似口径: 用 avg-cost + peak/trough 跟踪, 不引入逐笔 FIFO 复杂度(见 M1 计划)。
"""

import time
from typing import Any, Optional

from at01_common.logger import LoggerMixin


class TradingJournal(LoggerMixin):
    """成交结果日志"""

    async def record(self, record: dict[str, Any]) -> int | None:
        """记录一次成交闭环, 返回主键(失败返回 None)

        record 字段(来自 ExecutionEngine.on_trade_record):
            symbol, strategy, bucket, entry_ts, entry_price, exit_price,
            quantity, realized_pnl, peak_price, trough_price
        """
        from at01_common.database import AsyncSessionLocal
        from at01_common.models import ClosedTrade

        entry_ts = record.get("entry_ts") or 0.0
        exit_ts = time.time()
        entry_price = record.get("entry_price") or 0.0
        exit_price = record.get("exit_price") or 0.0
        quantity = record.get("quantity") or 0.0
        peak = record.get("peak_price") or 0.0
        trough = record.get("trough_price") or 0.0

        max_profit = (peak - entry_price) * quantity if (peak > entry_price > 0) else 0.0
        max_drawdown = (entry_price - trough) * quantity if (0 < trough < entry_price) else 0.0

        row = ClosedTrade(
            symbol=record.get("symbol", ""),
            strategy=record.get("strategy", ""),
            bucket=record.get("bucket", "trade"),
            entry_ts=entry_ts,
            exit_ts=exit_ts,
            entry_price=entry_price,
            exit_price=exit_price,
            quantity=quantity,
            realized_pnl=record.get("realized_pnl", 0.0),
            holding_seconds=exit_ts - entry_ts if entry_ts > 0 else 0.0,
            max_profit=max_profit,
            max_drawdown=max_drawdown,
            regime=record.get("regime", ""),
        )
        try:
            async with AsyncSessionLocal() as session:
                session.add(row)
                await session.commit()
                self.logger.info(
                    "成交日志已记录", symbol=row.symbol, bucket=row.bucket,
                    pnl=round(row.realized_pnl, 2),
                    holding=round(row.holding_seconds, 0),
                )
                return row.id
        except Exception:
            self.logger.exception("成交日志落库失败", symbol=record.get("symbol"))
            return None

    async def recent(self, symbol: str, limit: int = 20) -> list[dict[str, Any]]:
        """近期成交记录"""
        from sqlalchemy import select

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import ClosedTrade

        try:
            async with AsyncSessionLocal() as session:
                rows = (
                    (
                        await session.execute(
                            select(ClosedTrade)
                            .where(ClosedTrade.symbol == symbol)
                            .order_by(ClosedTrade.id.desc())
                            .limit(limit)
                        )
                    )
                    .scalars()
                    .all()
                )
                return [
                    {
                        "symbol": r.symbol, "strategy": r.strategy, "bucket": r.bucket,
                        "entry_price": r.entry_price, "exit_price": r.exit_price,
                        "quantity": r.quantity, "realized_pnl": r.realized_pnl,
                        "holding_seconds": r.holding_seconds,
                        "max_profit": r.max_profit, "max_drawdown": r.max_drawdown,
                        "regime": r.regime,
                    }
                    for r in rows
                ]
        except Exception:
            return []
