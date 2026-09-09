"""
订单恢复引擎(V10.7, P0-c)

周期收敛本地「结果未明 / 记账中断」的实盘订单, 按交易所真相(订单查询 + 逐笔成交)
确定性自愈, 使订单状态与本地账本最终一致, 消除「交易所已成交但本地仍 UNKNOWN /
账本已漂移」的窗口。

两类待恢复订单:
- UNKNOWN / SUBMITTING: 下单结果未明或请求在途。按 clientOrderId 反查交易所:
  查不到 -> 从未在交易所创建, 撤销(安全); 查到 FILLED -> 完整成交记账;
  查到 CANCELED/REJECTED/EXPIRED -> 撤销; 查到仍挂单 -> 回填交易所 ID + 状态跟踪。
- RECOVERY_REQUIRED: 交易所已成交但本地记账强一致事务失败(整体回滚)。
  BUY 确定性重建; SELL 从 DB 开仓 lot(权威未消费态)确定性重放 FIFO 分配重建
  (V11.1 P0-4, 消除人工冻结)。

幂等: 每次收敛后订单离开待恢复集合(状态终态 / accounting_state=RECOVERED),
重复扫描不重复记账。纸面模式不查交易所, 不实例化本模块。
"""

from typing import Any, Optional

from at01_common.logger import LoggerMixin


class OrderRecoveryEngine(LoggerMixin):
    """订单恢复引擎(实盘周期收敛)"""

    def __init__(
        self,
        rest_client: Any = None,
        execution_engine: Any = None,
        risk_manager: Any = None,
    ):
        self.rest = rest_client
        self.execution = execution_engine
        self.risk = risk_manager

    async def recover(self, symbol: str) -> list[dict[str, Any]]:
        """扫描待恢复订单并收敛, 返回未解决差异列表(空 = 全部收敛)"""
        if self.rest is None or self.execution is None:
            return []

        orders = await self._load_pending(symbol)
        if not orders:
            return []

        unresolved: list[dict[str, Any]] = []
        for o in orders:
            try:
                if not await self._recover_one(symbol, o):
                    unresolved.append({
                        "type": "recover_unresolved",
                        "symbol": symbol,
                        "client_order_id": o.get("client_order_id"),
                        "status": o.get("status"),
                        "accounting_state": o.get("accounting_state"),
                    })
            except Exception as e:
                self.logger.exception("订单恢复失败", client_order_id=o.get("client_order_id"))
                unresolved.append({
                    "type": "recover_error",
                    "symbol": symbol,
                    "client_order_id": o.get("client_order_id"),
                    "detail": str(e),
                })
        return unresolved

    # ---------- 内部 ----------

    async def _load_pending(self, symbol: str) -> list[dict[str, Any]]:
        """本地待恢复订单: 状态 UNKNOWN/SUBMITTING, 或 accounting_state=RECOVERY_REQUIRED(仅实盘)"""
        from sqlalchemy import or_, select

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import Order

        try:
            async with AsyncSessionLocal() as session:
                rows = (
                    await session.execute(
                        select(Order).where(
                            Order.symbol == symbol,
                            Order.is_paper.is_(False),
                            or_(
                                Order.status.in_(("UNKNOWN", "SUBMITTING")),
                                Order.accounting_state == "RECOVERY_REQUIRED",
                            ),
                        )
                    )
                ).scalars().all()
                return [{
                    "client_order_id": r.client_order_id,
                    "exchange_order_id": r.exchange_order_id,
                    "side": r.side,
                    "status": r.status,
                    "accounting_state": r.accounting_state,
                    "filled_quantity": r.filled_quantity,
                    "avg_fill_price": r.avg_fill_price,
                } for r in rows]
        except Exception:
            self.logger.exception("订单恢复加载本地订单失败")
            return []

    async def _recover_one(self, symbol: str, o: dict[str, Any]) -> bool:
        """恢复单个订单, 返回 True = 已收敛 / False = 仍需人工(未解决)"""
        if o.get("accounting_state") == "RECOVERY_REQUIRED":
            return await self._recover_accounting(symbol, o)
        return await self._recover_status(symbol, o)

    async def _recover_status(self, symbol: str, o: dict[str, Any]) -> bool:
        """UNKNOWN / SUBMITTING: 按 clientOrderId 反查交易所收敛"""
        cid = o.get("client_order_id")
        if not cid:
            return False
        rest = self.rest
        execution = self.execution
        if rest is None or execution is None:
            return False  # fail-closed: 依赖缺失, 返回未解决(保持禁开仓)
        try:
            detail = await rest.get_order(symbol, orig_client_order_id=cid)
        except Exception as e:
            if getattr(e, "code", None) == -2013:
                # 订单不存在: 从未在交易所创建(Binance 按 clientOrderId 查单是权威的) -> 撤销
                await self._mark_canceled(symbol, o)
                return True
            return False  # API 异常, 下次重试
        if not detail or not detail.get("orderId"):
            # 查不到 -> 从未在交易所创建 -> 撤销
            await self._mark_canceled(symbol, o)
            return True

        eid = str(detail["orderId"])
        status = str(detail.get("status", ""))
        if status == "FILLED":
            executed = float(detail.get("executedQty", 0) or 0)
            cum_quote = float(detail.get("cummulativeQuoteQty", 0) or 0)
            avg = cum_quote / executed if executed > 0 else float(detail.get("price", 0) or 0)
            result = await execution.apply_recovered_fill(
                symbol=symbol, side=o.get("side", "BUY"), client_order_id=cid,
                exchange_order_id=eid, fill_qty=executed, fill_price=avg, fee=0.0,
            )
            return result != "error"
        if status in ("CANCELED", "REJECTED", "EXPIRED"):
            # V11.0(F3): 终态前已部分成交(executedQty>0)须先记账, 否则部分成交被静默丢弃
            # (漏记账 -> 持仓/权益漂移); 无成交才走纯撤销。
            executed = float(detail.get("executedQty", 0) or 0)
            if executed > 0:
                cum_quote = float(detail.get("cummulativeQuoteQty", 0) or 0)
                avg = cum_quote / executed if executed > 0 else float(detail.get("price", 0) or 0)
                result = await execution.apply_recovered_fill(
                    symbol=symbol, side=o.get("side", "BUY"), client_order_id=cid,
                    exchange_order_id=eid, fill_qty=executed, fill_price=avg, fee=0.0,
                    final_status="CANCELED",
                )
                if result == "error":
                    return False
                return True
            await self._mark_canceled(symbol, o, exchange_order_id=eid)
            return True
        # 仍挂单(NEW / PARTIALLY_FILLED / OPEN): 回填交易所 ID + 状态, 交后续跟踪
        await execution._update_order_status(cid, status=status, exchange_order_id=eid)
        self.logger.info(
            "订单恢复: 已定位(仍挂单)", client_order_id=cid, status=status,
        )
        return True

    async def _recover_accounting(self, symbol: str, o: dict[str, Any]) -> bool:
        """RECOVERY_REQUIRED: 账务重建。BUY / SELL 均确定性自愈(V11.1 P0-4)。"""
        side = str(o.get("side", "")).upper()
        if side == "BUY":
            return await self._recover_buy_accounting(symbol, o)
        return await self._recover_sell_accounting(symbol, o)

    async def _recover_buy_accounting(self, symbol: str, o: dict[str, Any]) -> bool:
        cid = o.get("client_order_id")
        filled = float(o.get("filled_quantity") or 0.0)
        avg = float(o.get("avg_fill_price") or 0.0)
        eid = o.get("exchange_order_id") or ""
        rest = self.rest
        execution = self.execution
        if rest is None or execution is None:
            return False  # fail-closed: 依赖缺失, 返回未解决(保持禁开仓)

        # 本地未回填成交数据(异常)时, 从交易所真相补齐
        if filled <= 0:
            try:
                detail = await rest.get_order(symbol, orig_client_order_id=cid)
            except Exception as e:
                if getattr(e, "code", None) == -2013:
                    await self._mark_canceled(symbol, o)
                    return True
                return False
            if not detail or not detail.get("orderId"):
                await self._mark_canceled(symbol, o)
                return True
            eid = str(detail["orderId"])
            filled = float(detail.get("executedQty", 0) or 0)
            cum = float(detail.get("cummulativeQuoteQty", 0) or 0)
            avg = cum / filled if filled > 0 else 0.0

        if filled <= 0:
            # 无成交却标记恢复: 异常状态, 撤销
            await self._mark_canceled(symbol, o)
            return True

        result = await execution.rebuild_buy_accounting(
            symbol=symbol, client_order_id=cid, exchange_order_id=eid,
            fill_qty=filled, fill_price=avg, fee=0.0,
        )
        return result != "error"

    async def _recover_sell_accounting(self, symbol: str, o: dict[str, Any]) -> bool:
        """RECOVERY_REQUIRED SELL: 从 DB 开仓 lot 确定性重放 FIFO 分配重建(V11.1 P0-4)。"""
        cid = o.get("client_order_id")
        filled = float(o.get("filled_quantity") or 0.0)
        avg = float(o.get("avg_fill_price") or 0.0)
        eid = o.get("exchange_order_id") or ""
        rest = self.rest
        execution = self.execution
        if rest is None or execution is None:
            return False  # fail-closed: 依赖缺失, 返回未解决(保持禁开仓)

        # 本地未回填成交数据(异常)时, 从交易所真相补齐(与 BUY 同路径)
        if filled <= 0:
            try:
                detail = await rest.get_order(symbol, orig_client_order_id=cid)
            except Exception as e:
                if getattr(e, "code", None) == -2013:
                    await self._mark_canceled(symbol, o)
                    return True
                return False
            if not detail or not detail.get("orderId"):
                await self._mark_canceled(symbol, o)
                return True
            eid = str(detail["orderId"])
            filled = float(detail.get("executedQty", 0) or 0)
            cum = float(detail.get("cummulativeQuoteQty", 0) or 0)
            avg = cum / filled if filled > 0 else 0.0

        if filled <= 0:
            # 无成交却标记恢复: 异常状态, 撤销
            await self._mark_canceled(symbol, o)
            return True

        result = await execution.rebuild_sell_accounting(
            symbol=symbol, client_order_id=cid, exchange_order_id=eid,
            fill_qty=filled, fill_price=avg, fee=0.0,
        )
        return result != "error"

    async def _mark_canceled(
        self, symbol: str, o: dict[str, Any], exchange_order_id: Optional[str] = None,
    ) -> None:
        """本地订单在交易所已撤/拒/过期/从未创建 -> 改 CANCELED + 状态机回退"""
        cid = o.get("client_order_id")
        execution = self.execution
        if execution is None:
            return  # fail-closed: 无执行引擎, 无法改状态/回退状态机
        await execution._update_order_status(
            cid, status="CANCELED", exchange_order_id=exchange_order_id,
        )
        execution.trade_sm.on_order_canceled(
            symbol, o.get("side", "BUY"),
            self.risk.positions.get(symbol).quantity if self.risk else 0.0,
        )
        await execution.trade_sm.persist(symbol)
        await execution.events.log(
            event_type="CANCELED", client_order_id=cid, source="recovery",
        )
        self.logger.info("订单恢复: 已撤销", client_order_id=cid)
