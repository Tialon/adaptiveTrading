"""
Decision Journal(V4.0)

每次决策(含 HOLD)落库完整上下文, 供 AI 复盘:

    时间 / 价格 / 市场状态(含置信) / Alpha / 仓位(core/trade) /
    现金 / 权益 / 原因 / 指标快照

写入点: StrategyEngine 每次融合决策后。
"""

import json
from typing import Any, Optional

from at01_common.logger import LoggerMixin


class DecisionJournal(LoggerMixin):
    """决策日志"""

    async def log(
        self,
        symbol: str,
        action: str,
        price: float,
        quantity: float = 0.0,
        regime: str = "",
        regime_confidence: float = 0.0,
        alpha_score: float = 0.0,
        decision_score: float = 0.0,
        core_qty: float = 0.0,
        trade_qty: float = 0.0,
        cash: float = 0.0,
        equity: float = 0.0,
        reason: str = "",
        context: Optional[dict[str, Any]] = None,
    ) -> None:
        """写一条决策日志"""
        from at01_common.database import AsyncSessionLocal
        from at01_common.models import DecisionLog

        try:
            async with AsyncSessionLocal() as session:
                session.add(
                    DecisionLog(
                        symbol=symbol,
                        action=action,
                        price=price,
                        quantity=quantity,
                        regime=regime,
                        regime_confidence=regime_confidence,
                        alpha_score=alpha_score,
                        decision_score=decision_score,
                        core_qty=core_qty,
                        trade_qty=trade_qty,
                        cash=cash,
                        equity=equity,
                        reason=reason[:1000],
                        context=json.dumps(context, ensure_ascii=False, default=str)[:2000]
                        if context else None,
                    )
                )
                await session.commit()
        except Exception:
            self.logger.exception("决策日志写入失败")

    async def recent(self, symbol: str, limit: int = 50) -> list[dict[str, Any]]:
        """读取近期决策(复盘/展示)"""
        from sqlalchemy import select

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import DecisionLog

        try:
            async with AsyncSessionLocal() as session:
                rows = (
                    (
                        await session.execute(
                            select(DecisionLog)
                            .where(DecisionLog.symbol == symbol)
                            .order_by(DecisionLog.id.desc())
                            .limit(limit)
                        )
                    )
                    .scalars()
                    .all()
                )
                return [
                    {
                        "id": r.id, "action": r.action, "price": r.price,
                        "quantity": r.quantity, "regime": r.regime,
                        "regime_confidence": r.regime_confidence,
                        "alpha_score": r.alpha_score, "decision_score": r.decision_score,
                        "core_qty": r.core_qty, "trade_qty": r.trade_qty,
                        "cash": r.cash, "equity": r.equity,
                        "reason": r.reason, "created_at": str(r.created_at),
                    }
                    for r in rows
                ]
        except Exception:
            return []
