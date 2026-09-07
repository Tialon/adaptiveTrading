"""
启动对账(V10)

实盘启动时对崩溃窗口做恢复: 进程可能在下单后、成交确认前崩溃, 导致
本地订单停留在 NEW/PARTIALLY_FILLED 而交易所已成交。启动时拉交易所挂单
与成交历史, 做**确定性自愈**(交易所已 FILLED -> 本地订单改 FILLED + 状态机推进);
其余歧义(孤儿挂单/无法匹配)记入未解决差异列表, 由上层冻结(急停)。

纸面模式不查交易所(仅靠现有现金自检), 不实例化本模块。
"""

from typing import Any, Optional

from at01_common.logger import LoggerMixin


class StartupReconciler(LoggerMixin):
    """启动崩溃窗口恢复 + 订单歧义检测"""

    def __init__(
        self,
        rest_client: Any = None,
        execution_engine: Any = None,
        risk_manager: Any = None,
    ):
        self.rest = rest_client
        self.execution = execution_engine
        self.risk = risk_manager

    async def reconcile(self, symbol: str) -> list[dict[str, Any]]:
        """返回未解决差异列表(空=通过, 无歧义)"""
        if self.rest is None:
            return []

        try:
            open_orders = await self.rest.get_open_orders(symbol)
        except Exception as e:
            self.logger.warning("启动对账获取挂单失败", error=str(e))
            return [{"type": "api_error", "symbol": symbol, "detail": str(e)}]

        try:
            # V11.0(F10): 优先分页拉全近期成交(不因 limit=100 截断漏掉崩溃窗口成交);
            # V11.1(P0-1): get_my_trades_all 现返回 MyTradesResult, 解包 .trades(兼容旧 list);
            # 测试/降级用 rest 无 get_my_trades_all 时回退单页 get_my_trades。
            get_all = getattr(self.rest, "get_my_trades_all", None)
            if get_all is not None:
                result = await get_all(symbol)
                my_trades = result.trades if hasattr(result, "trades") else result
            else:
                my_trades = await self.rest.get_my_trades(symbol, limit=100)
        except Exception as e:
            self.logger.warning("启动对账获取成交历史失败", error=str(e))
            my_trades = []

        open_ids = {str(o.get("orderId")) for o in open_orders}
        trade_ids = {str(t.get("orderId")) for t in my_trades}

        local_open = await self._load_local_open_orders(symbol)
        unresolved: list[dict[str, Any]] = []

        for order in local_open:
            eid = order.get("exchange_order_id")
            if not eid:
                # UNKNOWN/SUBMITTING 单(下单结果未明/在途)可尝试按 clientOrderId 反查收敛
                if order.get("status") in ("UNKNOWN", "SUBMITTING") and await self._resolve_unknown(symbol, order):
                    continue
                # 下单前崩溃(无交易所订单 ID), 无法匹配 -> 歧义
                unresolved.append({
                    "type": "no_exchange_id",
                    "symbol": symbol,
                    "client_order_id": order.get("client_order_id"),
                })
                continue
            if eid in open_ids:
                # 交易所仍挂单(未成交), 属正常状态, 保留
                continue
            if eid in trade_ids:
                # 交易所已成交(不在挂单、在成交历史) -> 确定性自愈
                await self._self_heal_filled(symbol, order, eid)
                continue
            # 不在挂单也不在成交历史 -> 查单确认终态
            try:
                detail = await self.rest.get_order(symbol, eid)
            except Exception:
                unresolved.append({
                    "type": "ambiguous_order",
                    "symbol": symbol,
                    "exchange_order_id": eid,
                })
                continue
            status = str(detail.get("status", ""))
            if status == "FILLED":
                await self._self_heal_filled(symbol, order, eid)
            elif status in ("CANCELED", "REJECTED", "EXPIRED"):
                await self._mark_canceled(order)
            else:
                unresolved.append({
                    "type": "ambiguous_order",
                    "symbol": symbol,
                    "exchange_order_id": eid,
                    "status": status,
                })

        # 交易所挂单无本地匹配 -> 孤儿单(歧义, 不自动改账)
        local_eids = {o.get("exchange_order_id") for o in local_open}
        for o in open_orders:
            if str(o.get("orderId")) not in local_eids:
                unresolved.append({
                    "type": "orphan_exchange_order",
                    "symbol": symbol,
                    "exchange_order_id": str(o.get("orderId")),
                })

        return unresolved

    # ---------- 内部 ----------

    async def _load_local_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        """本地非终态订单(NEW / PARTIALLY_FILLED / UNKNOWN / SUBMITTING, 仅实盘)"""
        from sqlalchemy import select

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import Order

        try:
            async with AsyncSessionLocal() as session:
                rows = (
                    await session.execute(
                        select(Order).where(
                            Order.symbol == symbol,
                            Order.is_paper.is_(False),
                            Order.status.in_(("NEW", "PARTIALLY_FILLED", "UNKNOWN", "SUBMITTING")),
                        )
                    )
                ).scalars().all()
                return [{
                    "client_order_id": r.client_order_id,
                    "exchange_order_id": r.exchange_order_id,
                    "side": r.side,
                    "status": r.status,
                } for r in rows]
        except Exception:
            self.logger.exception("启动对账加载本地订单失败")
            return []

    async def _self_heal_filled(self, symbol: str, order: dict[str, Any], eid: str) -> None:
        """确定性自愈: 本地非终态订单在交易所已成交 -> 完整记账 + 状态机推进

        V11.0(F5): 原实现只改订单状态 + 推进状态机、不重放成交记账, 导致崩溃窗口内
        「交易所已成交但本地持仓/lot/账本未落」的永久漂移(权益对账只能发现、不能自愈)。
        改为复用 apply_recovered_fill 做完整记账(内存 + DB 镜像), 与订单恢复引擎口径一致。
        """
        executed = 0.0
        avg_price = 0.0
        status = "FILLED"
        try:
            detail = await self.rest.get_order(symbol, eid)
            executed = float(detail.get("executedQty", 0) or 0)
            cum_quote = float(detail.get("cummulativeQuoteQty", 0) or 0)
            avg_price = cum_quote / executed if executed > 0 else 0.0
            status = str(detail.get("status", "") or "FILLED")
        except Exception:
            pass

        if executed <= 0:
            # 交易所无成交 -> 无账可记, 保守撤销(理论不应走到)
            await self._mark_canceled(order)
            return

        if self.execution is not None:
            # 部分成交后撤/拒/过期 -> 终态落 CANCELED 而非 FILLED(F3 口径)
            final_status = "CANCELED" if status in ("CANCELED", "REJECTED", "EXPIRED") else "FILLED"
            await self.execution.apply_recovered_fill(
                symbol=symbol, side=str(order.get("side", "BUY")).upper(),
                client_order_id=order["client_order_id"],
                exchange_order_id=eid, fill_qty=executed, fill_price=avg_price,
                final_status=final_status,
            )
        self.logger.info(
            "启动自愈: 订单已成交并记账", symbol=symbol,
            exchange_order_id=eid, qty=executed, status=status,
        )

    async def _mark_canceled(self, order: dict[str, Any]) -> None:
        """本地非终态订单在交易所已撤/拒/过期 -> 改 CANCELED"""
        if self.execution is not None:
            await self.execution._update_order_status(order["client_order_id"], status="CANCELED")
        self.logger.info("启动对账: 订单已撤/拒", client_order_id=order.get("client_order_id"))

    async def _resolve_unknown(self, symbol: str, order: dict[str, Any]) -> bool:
        """UNKNOWN 订单(无交易所 ID)按 clientOrderId 反查交易所收敛; 收敛成功返回 True

        - 查到 FILLED -> 复用 _self_heal_filled 自愈
        - 查到 CANCELED/REJECTED/EXPIRED -> 复用 _mark_canceled
        - 查到仍挂单(OPEN/PARTIALLY_FILLED/NEW) -> 回填交易所 ID + 状态, 交后续对账跟踪
        - 查不到 -> 返回 False(计入未解决差异, 触发急停)
        """
        client_id = order.get("client_order_id")
        if not client_id:
            return False
        try:
            detail = await self.rest.get_order(symbol, orig_client_order_id=client_id)
        except Exception:
            return False
        eid = str(detail.get("orderId") or "")
        if not eid:
            return False
        status = str(detail.get("status", ""))
        if status == "FILLED":
            await self._self_heal_filled(symbol, order, eid)
            return True
        if status in ("CANCELED", "REJECTED", "EXPIRED"):
            await self._mark_canceled(order)
            return True
        if self.execution is not None:
            await self.execution._update_order_status(
                client_id, status=status, exchange_order_id=eid
            )
        self.logger.info(
            "启动对账: UNKNOWN 订单已定位(仍挂单)", client_order_id=client_id, status=status
        )
        return True
