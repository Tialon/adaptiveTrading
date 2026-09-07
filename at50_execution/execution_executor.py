"""
执行引擎

接收风控批准后的信号:
1. 生成订单记录(Order 表)
2. 纸面模式 -> PaperBroker 成交;实盘模式 -> Binance REST 下单
3. 成交确认(轮询/即时),更新订单与持仓
4. 回调(策略 on_fill / 主编排器推送)
"""

import time
import uuid
from decimal import Decimal
from typing import Any, Awaitable, Callable, Optional

from at01_common.settings import get_settings
from at01_common.logger import LoggerMixin
from at50_execution.execution_paper_broker import PaperBroker
from at50_execution.execution_state import TradeStateMachine
from at60_risk.risk_manager import RiskManager
from at50_strategy.strategy_base import Signal

FillCallback = Callable[[Signal, float, float], Awaitable[None]]
TradeRecordCallback = Callable[[dict], Awaitable[None]]  # V9.0: 成交闭环回调(journal)


class ExecutionEngine(LoggerMixin):
    """执行引擎"""

    def __init__(
        self,
        risk_manager: RiskManager,
        rest_client: Any = None,
        on_fill: Optional[FillCallback] = None,
        portfolio: Any = None,  # V3.0: PortfolioEngine
    ):
        self.settings = get_settings()
        self.risk = risk_manager
        self.rest = rest_client  # market.rest_client.BinanceRestClient
        self.on_fill = on_fill
        self.portfolio = portfolio
        self.on_signal_registered = None  # V3.0: callable(signal_id, signal) 信号落库后回调(tracker 注册)
        self.on_trade_record = None  # V9.0: callable(record: dict) 成交闭环回调(TradingJournal)

        self.paper = PaperBroker(
            initial_cash=self.settings.paper_initial_cash,
            fee_rate=self.settings.paper_fee_rate,
        )
        self.is_paper = self.settings.paper_trading
        self.order_count = 0
        self.error_count = 0
        # V2.0: 幂等控制(近期已执行的 策略:标的:方向 组合)
        self._recent_executed: dict[str, float] = {}
        self.idempotency_seconds: float = 10.0  # 同组合冷却秒数
        # V3.0: 交易状态机(防重复建仓)
        self.trade_sm = TradeStateMachine()

    # ---------- 主入口 ----------

    async def execute(self, signal: Signal) -> Optional[dict[str, Any]]:
        """执行已批准的信号(V2.0: 幂等 + 完整流程)

        流程: 生成订单 -> 保存数据库 -> 发送交易所 -> 监听成交 -> 更新持仓 -> 记录结果
        """
        # 幂等: 同策略同方向同价的近期信号直接跳过(防重复买入)
        idem_key = f"{signal.strategy}:{signal.symbol}:{signal.side.value}"
        last = self._recent_executed.get(idem_key)
        now = time.time()
        if last is not None and now - last < self.idempotency_seconds:
            self.logger.warning(
                "幂等拦截: 重复信号", key=idem_key, within=round(now - last, 1)
            )
            return None
        self._recent_executed[idem_key] = now

        # V9.0: 核心仓信号(低频组合再平衡)不走交易周期状态机
        is_core = getattr(signal, "bucket", "trade") == "core"

        # V3.0: 交易状态机闸门(ENTRY_PENDING/HOLDING 期间拒绝重复买入)
        if not is_core and signal.side.value == "BUY" and not self.trade_sm.can_buy(signal.symbol):
            self.logger.warning(
                "交易状态机拦截买入", symbol=signal.symbol,
                state=self.trade_sm.get(signal.symbol).value,
            )
            return None

        self.order_count += 1
        client_order_id = f"at-{int(time.time()*1000)}-{uuid.uuid4().hex[:8]}"

        # 1. 生成订单并保存数据库
        sig_id = await self._create_order_record(signal, client_order_id)
        # V3.0: 通知信号跟踪器注册
        if sig_id is not None and self.on_signal_registered:
            try:
                self.on_signal_registered(sig_id, signal)
            except Exception:
                self.logger.exception("信号注册回调异常")

        # 2. 推进交易状态机到挂单中(ENTRY_PENDING / EXIT_PENDING)
        if not is_core:
            self.trade_sm.on_order_submitted(signal.symbol, signal.side.value)
            await self.trade_sm.persist(signal.symbol)

        try:
            if self.is_paper:
                result = await self._execute_paper(signal, client_order_id)
            else:
                result = await self._execute_live(signal, client_order_id)
        except Exception as e:
            self.error_count += 1
            self.risk.record_execution_error()
            self.logger.error("执行异常", symbol=signal.symbol, error=str(e))
            await self._update_order_status(client_order_id, status="REJECTED", error_msg=str(e))
            # 回退状态机(买单未成交回 IDLE, 卖单回 HOLDING)
            if not is_core:
                self.trade_sm.on_order_canceled(
                    signal.symbol, signal.side.value,
                    self.risk.positions.get(signal.symbol).quantity,
                )
                await self.trade_sm.persist(signal.symbol)
            return None

        if result is None:
            # 纸面资金不足等: 未成交, 回退状态机(买单 ENTRY_PENDING -> IDLE)
            if not is_core:
                self.trade_sm.on_order_canceled(
                    signal.symbol, signal.side.value,
                    self.risk.positions.get(signal.symbol).quantity,
                )
                await self.trade_sm.persist(signal.symbol)
            return None

        status, fill_qty, fill_price, fee = result
        self.risk.record_execution_success()

        # 3. 更新订单状态(数据库)
        await self._update_order_status(
            client_order_id,
            status=status,
            filled_quantity=fill_qty,
            avg_fill_price=fill_price,
        )

        # 4. 终态未成交(取消/拒绝/过期, 或零成交) -> 状态机回退
        if status in ("CANCELED", "REJECTED", "EXPIRED") or fill_qty <= 0:
            if not is_core:
                self.trade_sm.on_order_canceled(
                    signal.symbol, signal.side.value,
                    self.risk.positions.get(signal.symbol).quantity,
                )
                await self.trade_sm.persist(signal.symbol)
            return {
                "client_order_id": client_order_id,
                "status": status,
                "fill_qty": fill_qty,
                "fill_price": fill_price,
            }

        # 5. 成交(FILLED / PARTIALLY_FILLED 且 qty>0) -> 更新持仓 -> 状态机推进 -> 记录结果
        realized = 0.0
        pre_sell = self.risk.positions.get(signal.symbol)  # V9.0: 卖出前快照(供成交日志)
        # V3.0: 经 Portfolio Engine 记账(含成本曲线/降本计算)
        if self.portfolio is not None:
            if signal.side.value == "BUY":
                self.portfolio.on_buy_fill(signal.symbol, fill_qty, fill_price)
            else:
                realized, _ = self.portfolio.on_sell_fill(signal.symbol, fill_qty, fill_price, fee)
            pos = self.risk.positions.get(signal.symbol)
        else:
            if signal.side.value == "BUY":
                pos = self.risk.positions.apply_buy(signal.symbol, fill_qty, fill_price, fee)
            else:
                pos, realized = self.risk.positions.apply_sell(signal.symbol, fill_qty, fill_price, fee)

        await self.risk.positions.persist(signal.symbol)

        # V3.0: 交易状态机推进(持仓已更新, remaining 为最新值)
        remaining = self.risk.positions.get(signal.symbol).quantity
        if not is_core:
            self.trade_sm.on_order_filled(signal.symbol, signal.side.value, remaining)
            await self.trade_sm.persist(signal.symbol)

        # V9.0: 成交闭环日志(SELL 记录一次完整交易, 供 AI 复盘)
        if signal.side.value == "SELL" and fill_qty > 0 and self.on_trade_record:
            try:
                await self.on_trade_record({
                    "symbol": signal.symbol,
                    "strategy": signal.source_strategy or signal.strategy,
                    "bucket": getattr(signal, "bucket", "trade"),
                    "entry_ts": pre_sell.entry_ts,
                    "entry_price": pre_sell.avg_price,
                    "exit_price": fill_price,
                    "quantity": fill_qty,
                    "realized_pnl": realized,
                    "peak_price": pre_sell.peak_price,
                    "trough_price": pre_sell.trough_price,
                })
            except Exception:
                self.logger.exception("成交日志回调异常")

        # 策略绩效记录(按单笔已实现盈亏, 非累计值)
        await self._record_strategy_performance(signal, realized)

        # 通知策略
        if self.on_fill:
            await self.on_fill(signal, fill_price, fill_qty)

        return {
            "client_order_id": client_order_id,
            "status": status,
            "fill_qty": fill_qty,
            "fill_price": fill_price,
            "position": pos.to_dict(),
        }

    # ---------- 纸面执行 ----------

    async def _execute_paper(
        self, signal: Signal, client_order_id: str
    ) -> Optional[tuple[str, float, float, float]]:
        """纸面交易执行(返回 status, qty, price, fee)"""
        last_price = signal.price
        order = await self.paper.create_order(
            symbol=signal.symbol,
            side=signal.side.value,
            order_type="MARKET",  # 纸面简化:直接按当前价成交
            quantity=signal.quantity or 0.0,
            price=None,
            last_price=last_price,
            client_order_id=client_order_id,
        )
        if order.status == "REJECTED":
            await self._update_order_status(client_order_id, status="REJECTED", error_msg="资金不足")
            return None
        return order.status, order.filled_quantity, order.avg_fill_price, order.fee_paid

    # ---------- 实盘执行 ----------

    async def _execute_live(
        self, signal: Signal, client_order_id: str
    ) -> Optional[tuple[str, float, float, float]]:
        """实盘执行(限价单+轮询成交确认,重试)"""
        if self.rest is None:
            raise RuntimeError("实盘模式需要 REST 客户端")

        order_type = self.settings.execution_order_type.upper()
        slip = self.settings.execution_price_slip_bps / 10000.0

        price: Optional[Decimal] = None
        if order_type == "LIMIT":
            raw = signal.price * (1 + slip) if signal.side.value == "BUY" else signal.price * (1 - slip)
            price = Decimal(str(round(raw, 2)))

        last_error: Optional[Exception] = None
        for attempt in range(1, self.settings.execution_max_retry + 1):
            try:
                resp = await self.rest.create_order(
                    symbol=signal.symbol,
                    side=signal.side.value,
                    order_type=order_type,
                    quantity=Decimal(str(signal.quantity)),
                    price=price,
                    new_client_order_id=client_order_id,
                )
                exchange_order_id = str(resp["orderId"])

                # 成交确认轮询
                status, qty, price = await self._confirm_fill(signal, exchange_order_id)
                return status, qty, price, 0.0  # 实盘手续费待对账, 此处记 0
            except Exception as e:
                last_error = e
                self.logger.warning(
                    "下单失败重试", attempt=attempt, symbol=signal.symbol, error=str(e)
                )
                await __import__("asyncio").sleep(1.0 * attempt)

        raise RuntimeError(f"下单重试耗尽: {last_error}")

    async def _confirm_fill(
        self, signal: Signal, exchange_order_id: str
    ) -> tuple[str, float, float]:
        """轮询确认成交(保留部分成交, 撤单前不回吐已成交部分)"""
        deadline = time.time() + 60.0
        last_executed = 0.0
        last_avg = signal.price
        while time.time() < deadline:
            order = await self.rest.get_order(signal.symbol, exchange_order_id)
            status = order.get("status", "NEW")
            executed_qty = float(order.get("executedQty", 0))
            cum_quote = float(order.get("cummulativeQuoteQty", 0) or 0)
            avg_price = cum_quote / executed_qty if executed_qty > 0 else signal.price
            if status == "FILLED":
                return status, executed_qty, avg_price
            if status == "PARTIALLY_FILLED":
                # 记录部分成交, 继续轮询直至 FILLED / 终态 / 超时
                last_executed = executed_qty
                last_avg = avg_price
            elif status in ("CANCELED", "REJECTED", "EXPIRED"):
                # 终态前已部分成交: 保留已成交部分, 不回吐
                if executed_qty > 0:
                    return "PARTIALLY_FILLED", executed_qty, avg_price
                return status, 0.0, signal.price
            await __import__("asyncio").sleep(self.settings.execution_fill_poll_seconds)

        # 超时撤单(保留已成交部分)
        try:
            await self.rest.cancel_order(signal.symbol, exchange_order_id)
        except Exception:
            pass
        if last_executed > 0:
            return "PARTIALLY_FILLED", last_executed, last_avg
        return "CANCELED", 0.0, signal.price

    # ---------- 策略绩效(V2.0) ----------

    async def _record_strategy_performance(self, signal: Signal, realized: float) -> None:
        """按策略累计绩效(strategy_performance 表, 供 AI 优化)

        realized 为本次成交的已实现盈亏(买入为 0), 累加而非读取累计值(避免重复累加)。
        """
        from sqlalchemy import select

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import StrategyPerformance

        try:
            async with AsyncSessionLocal() as session:
                row = (
                    await session.execute(
                        select(StrategyPerformance).where(
                            StrategyPerformance.strategy == signal.strategy,
                            StrategyPerformance.symbol == signal.symbol,
                        )
                    )
                ).scalar_one_or_none()
                if row is None:
                    row = StrategyPerformance(strategy=signal.strategy, symbol=signal.symbol)
                    session.add(row)
                    row.trade_count = 0
                    row.win_count = 0
                    row.profit = 0.0
                row.trade_count += 1
                if signal.side.value == "SELL":
                    row.profit += realized
                    if realized > 0:
                        row.win_count += 1
                row.win_rate = (
                    row.win_count / row.trade_count if row.trade_count > 0 else 0.0
                )
                await session.commit()
        except Exception:
            self.logger.exception("策略绩效落库失败")

    # ---------- 订单记录 ----------

    async def _create_order_record(self, signal: Signal, client_order_id: str) -> int | None:
        """落库新订单(V2.0: 含原因),返回订单主键"""
        import json as _json

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import Order, Signal as SignalModel

        try:
            async with AsyncSessionLocal() as session:
                # 同步信号状态
                sig_row = SignalModel(
                    symbol=signal.symbol,
                    strategy=signal.strategy,
                    side=signal.side.value,
                    price=signal.price,
                    quantity=signal.quantity,
                    quote_amount=signal.quote_amount,
                    reason=signal.reason_str[:500],
                    score=signal.score,
                    indicators=_json.dumps(signal.indicators, ensure_ascii=False)[:2000],
                    status="executing",
                )
                session.add(sig_row)
                await session.flush()
                order = Order(
                    client_order_id=client_order_id,
                    symbol=signal.symbol,
                    side=signal.side.value,
                    order_type=self.settings.execution_order_type,
                    price=signal.price,
                    quantity=signal.quantity or 0.0,
                    status="NEW",
                    strategy=signal.strategy,
                    signal_id=sig_row.id,
                    is_paper=self.is_paper,
                )
                session.add(order)
                await session.commit()
                return order.id
        except Exception:
            self.logger.exception("订单落库失败")
            return None

    async def _update_order_status(
        self,
        client_order_id: str,
        status: str,
        filled_quantity: Optional[float] = None,
        avg_fill_price: Optional[float] = None,
        exchange_order_id: Optional[str] = None,
        error_msg: Optional[str] = None,
    ) -> None:
        """更新订单状态"""
        from sqlalchemy import update

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import Order

        values: dict[str, Any] = {"status": status}
        if filled_quantity is not None:
            values["filled_quantity"] = filled_quantity
        if avg_fill_price is not None:
            values["avg_fill_price"] = avg_fill_price
        if exchange_order_id:
            values["exchange_order_id"] = exchange_order_id
        if error_msg:
            values["error_msg"] = error_msg[:500]

        try:
            async with AsyncSessionLocal() as session:
                await session.execute(
                    update(Order).where(Order.client_order_id == client_order_id).values(**values)
                )
                await session.commit()
        except Exception:
            self.logger.exception("订单状态更新失败")

    # ---------- 状态 ----------

    def status(self) -> dict[str, Any]:
        return {
            "mode": "paper" if self.is_paper else "live",
            "order_count": self.order_count,
            "error_count": self.error_count,
            "trade_states": self.trade_sm.status(),
            "paper": self.paper.status(),
        }

    async def close(self) -> None:
        """清理"""
        return
