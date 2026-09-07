"""
账户审计账本写入器(V9.0 M3.1)

每笔成交落 2 行余额变更:
- USDT 现金行: before/change/after(BUY 减现金, SELL 增现金)
- SOL 持仓行: before/change/after(BUY 增持仓, SELL 减持仓)

与 PortfolioLedger 的区别:
- PortfolioLedger 是内存账本, 维护双仓加权成本 + 对账不变量。
- 本表是持久化审计账本, 落库逐笔余额变更, 供盈亏溯源 / 多账户对账。

失败降级: 落库异常仅记录日志, 不影响主成交路径。
"""

from typing import Any, Optional

from at01_common.logger import LoggerMixin


class AccountLedgerWriter(LoggerMixin):
    """账户审计账本写入器"""

    async def record(
        self,
        *,
        ts: int,
        symbol: str,
        bucket: str,
        side: str,
        cash_before: float,
        cash_after: float,
        pos_before: float,
        pos_after: float,
        reason: str = "",
        related_order_id: str = "",
        commission: float = 0.0,
        commission_asset: str = "",
    ) -> bool:
        """单会话写 2 行(USDT 现金 + SOL 持仓), 返回是否成功。

        - cash_before/cash_after: 现金余额(成交前/后)
        - pos_before/pos_after: 该标的持仓数量(成交前/后)
        - commission/commission_asset: 本笔手续费(quote 口径, V10.1 真实成交摄入)
        """
        from at01_common.database import AsyncSessionLocal
        from at01_common.models import AccountLedger

        cash_change = cash_after - cash_before
        pos_change = pos_after - pos_before
        try:
            async with AsyncSessionLocal() as session:
                session.add(AccountLedger(
                    ts=ts, symbol=symbol, bucket=bucket, side=side,
                    asset="USDT",
                    before_amount=cash_before, change_amount=cash_change,
                    after_amount=cash_after,
                    commission=commission, commission_asset=commission_asset,
                    reason=reason[:500], related_order_id=related_order_id,
                ))
                session.add(AccountLedger(
                    ts=ts, symbol=symbol, bucket=bucket, side=side,
                    asset="SOL",
                    before_amount=pos_before, change_amount=pos_change,
                    after_amount=pos_after,
                    commission=commission, commission_asset=commission_asset,
                    reason=reason[:500], related_order_id=related_order_id,
                ))
                await session.commit()
            return True
        except Exception:
            self.logger.exception("账户审计账本落库失败", symbol=symbol, side=side)
            return False
