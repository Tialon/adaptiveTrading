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
        realized_pnl: float = 0.0,
        matched_cost: float = 0.0,
        session=None,
    ) -> bool:
        """单会话写 2 行(USDT 现金 + SOL 持仓), 返回是否成功。

        - cash_before/cash_after: 现金余额(成交前/后)
        - pos_before/pos_after: 该标的持仓数量(成交前/后)
        - commission/commission_asset: 本笔手续费(quote 口径, V10.1 真实成交摄入)
        - realized_pnl/matched_cost: FIFO 已实现盈亏/匹配成本(V10.3, 仅 SELL 有值)
        - session: 传入时复用该会话(不提交、异常上抛, 供外部强一致事务)
        """
        from at01_common.database import AsyncSessionLocal
        from at01_common.models import AccountLedger

        cash_change = cash_after - cash_before
        pos_change = pos_after - pos_before

        def _add_rows(s) -> None:
            s.add(AccountLedger(
                ts=ts, symbol=symbol, bucket=bucket, side=side,
                asset="USDT",
                before_amount=cash_before, change_amount=cash_change,
                after_amount=cash_after,
                commission=commission, commission_asset=commission_asset,
                realized_pnl=realized_pnl, matched_cost=matched_cost,
                reason=reason[:500], related_order_id=related_order_id,
            ))
            s.add(AccountLedger(
                ts=ts, symbol=symbol, bucket=bucket, side=side,
                asset="SOL",
                before_amount=pos_before, change_amount=pos_change,
                after_amount=pos_after,
                commission=commission, commission_asset=commission_asset,
                realized_pnl=realized_pnl, matched_cost=matched_cost,
                reason=reason[:500], related_order_id=related_order_id,
            ))

        if session is not None:
            _add_rows(session)
            return True

        try:
            async with AsyncSessionLocal() as s:
                _add_rows(s)
                await s.commit()
            return True
        except Exception:
            self.logger.exception("账户审计账本落库失败", symbol=symbol, side=side)
            return False
