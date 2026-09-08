"""
执行引擎

接收风控批准后的信号:
1. 生成订单记录(Order 表)
2. 纸面模式 -> PaperBroker 成交;实盘模式 -> Binance REST 下单
3. 成交确认(轮询/即时),更新订单与持仓
4. 回调(策略 on_fill / 主编排器推送)
"""

import asyncio
import time
import uuid
from decimal import Decimal
from typing import Any, Awaitable, Callable, Optional

import aiohttp

from at01_common.settings import get_settings
from at01_common.logger import LoggerMixin
from at20_market.market_rest_client import BinanceAPIError
from at50_execution.execution_paper_broker import PaperBroker
from at50_execution.execution_state import TradeStateMachine
from at60_risk.risk_manager import RiskManager
from at50_strategy.strategy_base import Signal
from at50_strategy.strategy_group import group_of
from at60_risk.risk_account_ledger import AccountLedgerWriter
from at60_risk.risk_lot import LotTracker
from at50_execution.execution_events import ExecutionEventLogger
from at50_execution.fee_calculator import FeeCalculator

FillCallback = Callable[[Signal, float, float], Awaitable[None]]
TradeRecordCallback = Callable[[dict], Awaitable[None]]  # V9.0: 成交闭环回调(journal)


def _compute_fill_metrics(symbol: str, fills: list[dict[str, Any]]) -> tuple[float, float, bool]:
    """从 myTrades 逐笔成交合成 (真实均价, quote 手续费, 是否存在不可计价手续费)

    均价 = ΣquoteQty / Σqty; 手续费 = Σ(quote 资产的 commission) + Σ(base 资产的 commission × price)。
    V11.1(P0-2): 委托 FeeCalculator 统一计价; 非 USDT/SOL 计价手续费 -> fee_unpriced=True(降级),
    不再静默记为 0。
    """
    total_qty = 0.0
    total_quote = 0.0
    for f in fills:
        total_qty += float(f.get("qty", 0) or 0)
        total_quote += float(f.get("quoteQty", 0) or 0)
    avg = total_quote / total_qty if total_qty > 0 else 0.0
    fee = FeeCalculator(symbol).total(fills)
    return avg, fee.fee_quote, fee.unpriced


# 浮点清零阈值(lot 剩余量 / 持仓量), 与 risk_lot._EPS 同口径
_SELL_EPS = 1e-9


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
        # V11.5 P1-3: 显式标注可选回调类型(由 run.py 动态注入), 消除 mypy 的
        # 「回调恒为 None → 分支不可达」误报, 并让回调签名可被静态检查。
        self.on_signal_registered: Optional[Callable[..., Any]] = None  # V3.0: callable(signal_id, signal)
        self.on_trade_record: Optional[TradeRecordCallback] = None  # V9.0: callable(record: dict)

        self.paper = PaperBroker(
            initial_cash=self.settings.paper_initial_cash,
            fee_rate=self.settings.paper_fee_rate,
        )
        self.account_ledger = AccountLedgerWriter()  # V9.0 M3.1: 审计账本
        self.lot_tracker = LotTracker()  # V10.3: FIFO 批次追踪(附加审计层)
        self.events = ExecutionEventLogger()  # V10.7: 订单执行事件日志(append-only 审计)
        self._filters: dict[str, Any] = {}  # V10.5: symbol -> SymbolFilters(惰性加载)
        self._accounting_locks: dict[str, asyncio.Lock] = {}  # V10.6: symbol -> 记账互斥锁
        self.is_paper = self.settings.paper_trading
        self.order_count = 0
        self.error_count = 0
        # V10.1: 幂等控制改为 DB 持久化(order_intents 唯一键), 键含 quantity + 时间桶, 重启不失效。
        # idempotency_seconds 作时间桶宽度。
        self.idempotency_seconds: float = 10.0
        # V3.0: 交易状态机(防重复建仓)
        self.trade_sm = TradeStateMachine()

    # ---------- 主入口 ----------

    async def execute(self, signal: Signal) -> Optional[dict[str, Any]]:
        """执行已批准的信号(V2.0: 幂等 + 完整流程)

        流程: 生成订单 -> 保存数据库 -> 发送交易所 -> 监听成交 -> 更新持仓 -> 记录结果
        """
        # V9.0: 核心仓信号(低频组合再平衡)不走交易周期状态机
        is_core = getattr(signal, "bucket", "trade") == "core"

        # V3.0: 交易状态机闸门(ENTRY_PENDING/HOLDING 期间拒绝重复买入)
        if not is_core and signal.side.value == "BUY" and not self.trade_sm.can_buy(signal.symbol):
            self.logger.warning(
                "交易状态机拦截买入", symbol=signal.symbol,
                state=self.trade_sm.get(signal.symbol).value,
            )
            return None

        # V10.6(P1-e): 执行数量与信号数量分离 —— signal.quantity 为策略原始意图(不可变),
        # exec_qty 为实际提交数量(经 REDUCE_ONLY 缩量 / 交易规则过滤调整)。不再原地改写信号。
        exec_qty = signal.quantity or 0.0

        # V10.5: REDUCE_ONLY —— 卖出不得超持仓(关掉风控审批到执行之间的竞态窗口)
        if signal.side.value == "SELL":
            available = self.risk.positions.get(signal.symbol).quantity
            if available <= 0:
                self.logger.warning("REDUCE_ONLY: 无持仓, 拒绝卖出", symbol=signal.symbol)
                return None
            if exec_qty > available:
                self.logger.warning(
                    "REDUCE_ONLY: 缩量至持仓", symbol=signal.symbol,
                    original=exec_qty, capped=available,
                )
                exec_qty = available

        # V10.1: 幂等(DB 持久化 order_intents 唯一键, 含 quantity + 时间桶, 重启不失效)
        idem_key = self._idempotency_key(signal)
        if not await self._register_intent(signal, idem_key):
            self.logger.warning("幂等拦截: 重复信号或注册失败", key=idem_key)
            return None

        self.order_count += 1
        client_order_id = f"at-{int(time.time()*1000)}-{uuid.uuid4().hex[:8]}"

        # 1. 生成订单并保存数据库。本地 Order 行落库失败则中止(fail-closed, 不投交易所):
        #    无 Order 行 -> 成交无法落 OrderFill、无法被恢复/对账追踪, 是「漏记账」路径。
        order_id = await self._create_order_record(signal, client_order_id, exec_qty)
        if order_id is None:
            self.error_count += 1
            self.risk.record_execution_error()
            self.logger.error(
                "订单落库失败, 中止执行(不投交易所)", symbol=signal.symbol,
            )
            await self._finalize_intent(idem_key, status="rejected")
            return None
        await self.events.log(
            event_type="ORDER_CREATED", client_order_id=client_order_id, order_id=order_id,
        )
        # V3.0: 通知信号跟踪器注册
        if self.on_signal_registered:
            try:
                self.on_signal_registered(order_id, signal)
            except Exception:
                self.logger.exception("信号注册回调异常")

        # 2. 推进交易状态机到挂单中(ENTRY_PENDING / EXIT_PENDING)
        if not is_core:
            self.trade_sm.on_order_submitted(signal.symbol, signal.side.value)
            await self.trade_sm.persist(signal.symbol)

        # V9.0 M3.1: 成交前快照(供审计账本 before/after)
        cash_before = self.paper.cash if self.is_paper else None
        pos_before = self.risk.positions.get(signal.symbol).quantity

        try:
            if self.is_paper:
                result = await self._execute_paper(signal, client_order_id, order_id, exec_qty)
            else:
                result = await self._execute_live(signal, client_order_id, order_id, exec_qty)
        except Exception as e:
            self.error_count += 1
            self.risk.record_execution_error()
            self.logger.error("执行异常", symbol=signal.symbol, error=str(e))
            # 纸面: 异常即失败(无交易所歧义)回退; 实盘: 结果未明 -> UNKNOWN 不回退
            if self.is_paper:
                await self._update_order_status(client_order_id, status="REJECTED", error_msg=str(e))
                await self._log_order_event(client_order_id, "REJECTED", order_id=order_id)
                if not is_core:
                    self.trade_sm.on_order_canceled(
                        signal.symbol, signal.side.value,
                        self.risk.positions.get(signal.symbol).quantity,
                    )
                    await self.trade_sm.persist(signal.symbol)
            else:
                await self._update_order_status(client_order_id, status="UNKNOWN", error_msg=str(e))
                await self._log_order_event(client_order_id, "UNKNOWN", order_id=order_id)
            await self._finalize_intent(idem_key, status="rejected")
            return None

        if result is None:
            # 纸面资金不足等: 未成交, 回退状态机(买单 ENTRY_PENDING -> IDLE)
            if not is_core:
                self.trade_sm.on_order_canceled(
                    signal.symbol, signal.side.value,
                    self.risk.positions.get(signal.symbol).quantity,
                )
                await self.trade_sm.persist(signal.symbol)
            await self._finalize_intent(idem_key, status="rejected")
            return None

        status, fill_qty, fill_price, fee, exchange_order_id = result
        if status in ("REJECTED", "UNKNOWN"):
            self.risk.record_execution_error()
        else:
            self.risk.record_execution_success()

        # 3. 更新订单状态(数据库); UNKNOWN 不回填成交数/均价(结果未明)。
        #    成交态(FILLED/PARTIALLY_FILLED)的状态/成交数据移入 _apply_fill_accounting 同事务
        #    (V11.0 F4: 消除「状态已 FILLED 但记账未落」的崩溃窗口); 非成交态在此先行落库。
        is_fill = status in ("FILLED", "PARTIALLY_FILLED") and fill_qty > 0
        if not is_fill:
            await self._update_order_status(
                client_order_id,
                status=status,
                filled_quantity=fill_qty if status != "UNKNOWN" else None,
                avg_fill_price=fill_price if status != "UNKNOWN" else None,
                exchange_order_id=exchange_order_id,
            )
        await self._log_order_event(
            client_order_id, status, exchange_order_id=exchange_order_id, order_id=order_id,
        )

        # 3.5 UNKNOWN: 不回退状态机(订单可能已成交), 交由对账收敛
        if status == "UNKNOWN":
            await self._finalize_intent(idem_key, status="rejected")
            return {
                "client_order_id": client_order_id,
                "status": status,
                "fill_qty": 0.0,
                "fill_price": signal.price,
            }

        # 4. 终态未成交(取消/拒绝/过期, 或零成交) -> 状态机回退
        if status in ("CANCELED", "REJECTED", "EXPIRED") or fill_qty <= 0:
            await self._finalize_intent(idem_key, status="rejected")
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

        # 5. 成交(FILLED / PARTIALLY_FILLED 且 qty>0) -> 本地记账(强一致事务) -> 状态机推进
        pre_sell = self.risk.positions.get(signal.symbol)  # V9.0: 卖出前快照(供成交日志)
        try:
            async with self._accounting_lock(signal.symbol):
                acct = await self._apply_fill_accounting(
                    signal, client_order_id, exchange_order_id,
                    fill_qty, fill_price, fee, pos_before, cash_before,
                    order_status=status,
                )
        except Exception:
            # 强一致失败: 整体回滚 + 标记 RECOVERY_REQUIRED + 急停冻结(不静默漂移)
            self.logger.exception(
                "成交后本地记账强一致事务失败, 已回滚并冻结",
                symbol=signal.symbol, client_order_id=client_order_id,
            )
            await self._mark_accounting_recovery_required(client_order_id)
            await self._finalize_intent(idem_key, status="executed")
            return {
                "client_order_id": client_order_id,
                "status": "RECOVERY_REQUIRED",
                "fill_qty": fill_qty,
                "fill_price": fill_price,
            }

        realized = acct["realized"]
        pos = acct["pos"]
        pos_after = acct["pos_after"]

        # V3.0: 交易状态机推进(持仓已更新, remaining 为最新值)
        remaining = pos_after
        if not is_core:
            self.trade_sm.on_order_filled(signal.symbol, signal.side.value, remaining)
            await self.trade_sm.persist(signal.symbol)

        # V9.0: 成交闭环日志(SELL 记录一次完整交易, 供 AI 复盘)
        if signal.side.value == "SELL" and fill_qty > 0 and self.on_trade_record:
            try:
                await self.on_trade_record({
                    "symbol": signal.symbol,
                    "strategy": group_of(signal.source_strategy or signal.strategy),
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

        await self._finalize_intent(idem_key, status="executed")

        return {
            "client_order_id": client_order_id,
            "status": status,
            "fill_qty": fill_qty,
            "fill_price": fill_price,
            "position": pos.to_dict(),
        }

    # ---------- 纸面执行 ----------

    async def _execute_paper(
        self, signal: Signal, client_order_id: str, order_id: Optional[int], exec_qty: float
    ) -> Optional[tuple[str, float, float, float, Optional[str]]]:
        """纸面交易执行(返回 status, qty, price, fee, exchange_order_id); 落一条合成成交明细(parity)"""
        last_price = signal.price
        order = await self.paper.create_order(
            symbol=signal.symbol,
            side=signal.side.value,
            order_type="MARKET",  # 纸面简化:直接按当前价成交
            quantity=exec_qty,
            price=None,
            last_price=last_price,
            client_order_id=client_order_id,
        )
        if order.status == "REJECTED":
            await self._update_order_status(client_order_id, status="REJECTED", error_msg="资金不足")
            return None
        # 纸面合成一条成交明细(让 fill -> fee -> ledger 路径在 paper 下同样被覆盖)
        if order.filled_quantity > 0:
            await self._record_fills(
                order_id=order_id, client_order_id=client_order_id,
                exchange_order_id=client_order_id,
                symbol=signal.symbol, side=signal.side.value,
                fills=[{
                    "id": 0,  # 纸面无交易所 tradeId, 用 0 作伪 id(唯一键 (client_order_id, 0))
                    "orderId": client_order_id,
                    "price": str(order.avg_fill_price),
                    "qty": str(order.filled_quantity),
                    "quoteQty": str(order.filled_quantity * order.avg_fill_price),
                    "commission": str(order.fee_paid),
                    "commissionAsset": "USDT",
                    "time": int(time.time() * 1000),
                }],
            )
        return order.status, order.filled_quantity, order.avg_fill_price, order.fee_paid, None

    # ---------- 实盘执行 ----------

    async def _execute_live(
        self, signal: Signal, client_order_id: str, order_id: Optional[int], exec_qty: float
    ) -> Optional[tuple[str, float, float, float, Optional[str]]]:
        """实盘执行(下单 + 异常分类 + 成交确认 + 真实手续费), 返回含 exchange_order_id 供启动对账匹配"""
        if self.rest is None:
            raise RuntimeError("实盘模式需要 REST 客户端")

        order_type = self.settings.execution_order_type.upper()
        slip = self.settings.execution_price_slip_bps / 10000.0

        price: Optional[Decimal] = None
        if order_type == "LIMIT":
            raw = signal.price * (1 + slip) if signal.side.value == "BUY" else signal.price * (1 - slip)
            price = Decimal(str(round(raw, 2)))

        # V10.5: 交易规则过滤(stepSize/tickSize/minQty/minNotional; 违规本地拒绝, 不发交易所)
        qty = exec_qty
        filters = await self._ensure_filters(signal.symbol)
        if filters is None:
            # V10.6(P1-f): 交易规则不可用(ExchangeInfo 失败)时禁 BUY —— 规则未知下开新仓
            # 风险不可控(safety 优先); SELL 减仓放行(不新增敞口, 失败无损失)。
            if signal.side.value == "BUY":
                self.logger.error(
                    "交易规则不可用, 禁 BUY(safety 优先)", symbol=signal.symbol,
                )
                await self._update_order_status(
                    client_order_id, status="REJECTED",
                    error_msg="exchangeInfo 不可用, 禁 BUY",
                )
                return "REJECTED", 0.0, signal.price, 0.0, None
        else:
            ref_price = price if price is not None else Decimal(str(signal.price))
            adj_qty, adj_price, violations = filters.adjust(
                Decimal(str(qty)), ref_price
            )
            if order_type == "LIMIT":
                price = adj_price
            if violations:
                msg = "; ".join(violations)
                self.logger.warning("交易规则过滤拒绝", symbol=signal.symbol, detail=msg)
                await self._update_order_status(
                    client_order_id, status="REJECTED", error_msg=msg
                )
                return "REJECTED", 0.0, signal.price, 0.0, None
            qty = float(adj_qty)

        # V10.2: 下单前标记 SUBMITTING(在途窗口, 供崩溃恢复区分「未下单」与「下单中」)
        await self._update_order_status(client_order_id, status="SUBMITTING")
        await self.events.log(event_type="SUBMITTING", client_order_id=client_order_id)

        request = {
            "symbol": signal.symbol,
            "side": signal.side.value,
            "order_type": order_type,
            "quantity": str(qty),
            "price": str(price) if price is not None else None,
            "new_client_order_id": client_order_id,
        }

        # 首次下单(V10.1: 异常分类, 不再盲重试; V10.2: 每次尝试落审计)
        try:
            resp = await self.rest.create_order(
                symbol=signal.symbol,
                side=signal.side.value,
                order_type=order_type,
                quantity=Decimal(str(qty)),
                price=price,
                new_client_order_id=client_order_id,
            )
        except BinanceAPIError as e:
            if e.status < 500:
                # 4xx 明确拒绝(订单未创建): REJECTED, 不重试
                await self._record_attempt(
                    order_id=order_id, client_order_id=client_order_id, attempt_no=1,
                    symbol=signal.symbol, side=signal.side.value, request=request,
                    response=f"{e.code}: {e.msg}", outcome="rejected",
                )
                await self._update_order_status(
                    client_order_id, status="REJECTED", error_msg=f"{e.code}: {e.msg}"
                )
                return "REJECTED", 0.0, signal.price, 0.0, None
            # 5xx: 结果未明 -> UNKNOWN 恢复
            await self._record_attempt(
                order_id=order_id, client_order_id=client_order_id, attempt_no=1,
                symbol=signal.symbol, side=signal.side.value, request=request,
                response=f"{e.code}: {e.msg}", outcome="ambiguous",
            )
            return await self._resolve_unknown(signal, client_order_id, order_id, price, order_type, request, qty)
        except (asyncio.TimeoutError, aiohttp.ClientError, OSError) as e:
            # 网络层: 请求可能已送达 -> UNKNOWN 恢复
            await self._record_attempt(
                order_id=order_id, client_order_id=client_order_id, attempt_no=1,
                symbol=signal.symbol, side=signal.side.value, request=request,
                response=str(e), outcome="ambiguous",
            )
            return await self._resolve_unknown(signal, client_order_id, order_id, price, order_type, request, qty)

        exchange_order_id = str(resp["orderId"])
        await self._record_attempt(
            order_id=order_id, client_order_id=client_order_id, attempt_no=1,
            symbol=signal.symbol, side=signal.side.value, request=request,
            response=str(resp), outcome="success", exchange_order_id=exchange_order_id,
        )
        await self.events.log(
            event_type="ORDER_ACK", client_order_id=client_order_id, exchange_order_id=exchange_order_id,
        )
        status, qty, price_filled, fee = await self._confirm_and_ingest(
            signal, client_order_id, order_id, exchange_order_id
        )
        return status, qty, price_filled, fee, exchange_order_id

    async def _ensure_filters(self, symbol: str):
        """惰性加载交易规则(live 模式); 拉取失败返回 None(不缓存, 下次重试), 由调用方决定处置"""
        from at50_execution.exchange_filters import SymbolFilters

        if symbol in self._filters:
            return self._filters[symbol]
        if self.rest is None:
            return None
        try:
            data = await self.rest.get_exchange_info(symbol)
            self._filters[symbol] = SymbolFilters.from_exchange_info(symbol, data)
        except Exception as e:
            self.logger.warning("交易规则拉取失败", symbol=symbol, error=str(e))
            return None
        return self._filters[symbol]

    async def _resolve_unknown(
        self,
        signal: Signal,
        client_order_id: str,
        order_id: Optional[int],
        price: Optional[Decimal],
        order_type: str,
        request: dict[str, Any],
        qty: float,
    ) -> tuple[str, float, float, float, Optional[str]]:
        """下单结果未明(超时/5xx): 反查交易所 -> 查到走成交确认; 查不到重试一次(同 clientOrderId 幂等);
        仍无法判定 -> UNKNOWN, 交由启动对账收敛。重试落 ExecutionAttempt(attempt_no=2)。"""
        # 1. 反查(可能已建单)
        detail = None
        try:
            detail = await self.rest.get_order(signal.symbol, orig_client_order_id=client_order_id)
        except Exception:
            detail = None
        if detail and detail.get("orderId"):
            eid = str(detail["orderId"])
            status, qty, price_filled, fee = await self._confirm_and_ingest(
                signal, client_order_id, order_id, eid
            )
            return status, qty, price_filled, fee, eid

        # 2. 未查到 -> 重试一次(同一 newClientOrderId, 币安服务端幂等)
        try:
            resp = await self.rest.create_order(
                symbol=signal.symbol,
                side=signal.side.value,
                order_type=order_type,
                quantity=Decimal(str(qty)),
                price=price,
                new_client_order_id=client_order_id,
            )
        except Exception as e:
            self.logger.error("下单重试仍失败, 订单状态未知", symbol=signal.symbol, error=str(e))
            await self._record_attempt(
                order_id=order_id, client_order_id=client_order_id, attempt_no=2,
                symbol=signal.symbol, side=signal.side.value, request=request,
                response=str(e), outcome="ambiguous",
            )
            await self._update_order_status(client_order_id, status="UNKNOWN", error_msg=str(e))
            return "UNKNOWN", 0.0, signal.price, 0.0, None

        eid = str(resp["orderId"])
        await self._record_attempt(
            order_id=order_id, client_order_id=client_order_id, attempt_no=2,
            symbol=signal.symbol, side=signal.side.value, request=request,
            response=str(resp), outcome="success", exchange_order_id=eid,
        )
        status, qty, price_filled, fee = await self._confirm_and_ingest(
            signal, client_order_id, order_id, eid
        )
        return status, qty, price_filled, fee, eid

    async def _confirm_and_ingest(
        self,
        signal: Signal,
        client_order_id: str,
        order_id: Optional[int],
        exchange_order_id: str,
    ) -> tuple[str, float, float, float]:
        """成交确认 + 真实成交明细摄入, 返回 (status, qty, avg_price, fee)"""
        status, qty, price = await self._confirm_fill(signal, exchange_order_id)
        fee = 0.0
        metrics = await self._ingest_fills(
            order_id=order_id, client_order_id=client_order_id,
            exchange_order_id=exchange_order_id, symbol=signal.symbol,
            side=signal.side.value,
        )
        if metrics is not None:
            price, fee, _ = metrics
        return status, qty, price, fee

    async def _confirm_fill(
        self, signal: Signal, exchange_order_id: str
    ) -> tuple[str, float, float]:
        """轮询确认成交(保留部分成交, 撤单前不回吐已成交部分)"""
        deadline = time.time() + 60.0
        last_executed = 0.0
        last_avg = signal.price
        while time.time() < deadline:
            try:
                order = await self.rest.get_order(signal.symbol, exchange_order_id)
            except Exception:
                # 查询瞬时失败(网络抖动): 继续轮询, 不误判
                await asyncio.sleep(self.settings.execution_fill_poll_seconds)
                continue
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
            await asyncio.sleep(self.settings.execution_fill_poll_seconds)

        # 超时撤单(保留已成交部分)
        canceled = False
        try:
            await self.rest.cancel_order(signal.symbol, exchange_order_id)
            canceled = True
        except Exception:
            pass
        if last_executed > 0:
            return "PARTIALLY_FILLED", last_executed, last_avg
        if canceled:
            return "CANCELED", 0.0, signal.price
        # 撤单也失败 -> 结果未明
        return "UNKNOWN", 0.0, signal.price

    # ---------- V10.1: 幂等 / 成交明细 ----------

    def _idempotency_key(self, signal: Signal) -> str:
        """幂等键: 策略:标的:方向:数量:时间桶

        含 quantity 区分不同量级的合法信号(评审 Signal A qty=1 / B qty=0.5);
        时间桶实现窗口语义(同桶去重, 跨桶放行), 与旧内存 10s 冷却一致但落库持久。
        """
        bucket = int(time.time() // max(self.idempotency_seconds, 0.001))
        return f"{signal.strategy}:{signal.symbol}:{signal.side.value}:{signal.quantity}:{bucket}"

    async def _register_intent(self, signal: Signal, idem_key: str) -> bool:
        """插入 OrderIntent 唯一键; 重复(IntegrityError)返回 False, 其余异常降级放行"""
        from sqlalchemy.exc import IntegrityError

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import OrderIntent

        try:
            async with AsyncSessionLocal() as session:
                session.add(OrderIntent(
                    idempotency_key=idem_key,
                    symbol=signal.symbol,
                    side=signal.side.value,
                    quantity=signal.quantity or 0.0,
                    price=signal.price,
                    status="pending",
                ))
                await session.commit()
            return True
        except IntegrityError:
            return False
        except Exception:
            # fail-closed: 幂等键无法落库则无法保证去重, 宁可放弃本信号也不重复下单
            # (无人值守安全优先; 信号丢失可由策略下一周期重新生成)。
            self.logger.exception("订单意图幂等注册失败(中止执行, 防重复下单)", key=idem_key)
            return False

    async def _finalize_intent(self, idem_key: str, status: str) -> None:
        """更新 OrderIntent 状态(pending -> executed/rejected)"""
        from sqlalchemy import update

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import OrderIntent

        try:
            async with AsyncSessionLocal() as session:
                await session.execute(
                    update(OrderIntent)
                    .where(OrderIntent.idempotency_key == idem_key)
                    .values(status=status)
                )
                await session.commit()
        except Exception:
            self.logger.exception("订单意图状态更新失败", key=idem_key)

    async def _ingest_fills(
        self,
        *,
        order_id: Optional[int],
        client_order_id: str,
        exchange_order_id: str,
        symbol: str,
        side: str,
    ) -> Optional[tuple[float, float, bool]]:
        """拉取 myTrades 逐笔成交, 落 OrderFill, 返回 (真实均价, quote 手续费, 是否有不可计价手续费);
        无明细返回 None。V11.1(P0-2): 不可计价手续费(fee_unpriced)触发降级(pause), 不静默记为 0。
        """
        fills: list[dict[str, Any]] = []
        try:
            # V11.0(F11): limit 提到 1000, 大单多笔成交(>50)不因默认 limit 截断漏手续费/漏成交。
            fills = await self.rest.get_my_trades(symbol, order_id=exchange_order_id, limit=1000)
        except Exception as e:
            self.logger.warning(
                "成交明细拉取失败", symbol=symbol, exchange_order_id=exchange_order_id, error=str(e)
            )
            return None
        if not fills:
            return None
        await self._record_fills(
            order_id=order_id, client_order_id=client_order_id,
            exchange_order_id=exchange_order_id, symbol=symbol, side=side, fills=fills,
        )
        avg, fee_quote, fee_unpriced = _compute_fill_metrics(symbol, fills)
        if fee_unpriced:
            self.logger.warning(
                "手续费不可计价(非 USDT/SOL), 账本手续费可能低估", symbol=symbol,
                exchange_order_id=exchange_order_id,
            )
            self.risk.pause(f"手续费不可计价 {symbol}")
        return avg, fee_quote, fee_unpriced

    async def _record_fills(
        self,
        *,
        order_id: Optional[int],
        client_order_id: str,
        exchange_order_id: Optional[str],
        symbol: str,
        side: str,
        fills: list[dict[str, Any]],
    ) -> int:
        """逐笔落 OrderFill(幂等: fill_idempotency_key 非空唯一), 返回新增行数"""
        from sqlalchemy import select

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import OrderFill

        added = 0
        try:
            async with AsyncSessionLocal() as session:
                # 已有幂等键(按本订单过滤, 有界): 成交ID缺失时 tid 落 'na', 仍可去重。
                existing: set[str] = set()
                if exchange_order_id:
                    rows = (
                        await session.execute(
                            select(OrderFill.fill_idempotency_key).where(
                                OrderFill.exchange_order_id == exchange_order_id
                            )
                        )
                    ).scalars().all()
                else:
                    rows = (
                        await session.execute(
                            select(OrderFill.fill_idempotency_key).where(
                                OrderFill.client_order_id == client_order_id
                            )
                        )
                    ).scalars().all()
                existing = {k for k in rows if k}
                calc = FeeCalculator(symbol)
                for f in fills:
                    tid = f.get("id")
                    tid_int = int(tid) if tid is not None else None
                    eoid = exchange_order_id or str(f.get("orderId") or "") or client_order_id
                    key = f"{eoid}:{tid_int if tid_int is not None else 'na'}"
                    if key in existing:
                        continue
                    ff = calc.fill_fee(f)
                    session.add(OrderFill(
                        order_id=order_id,
                        client_order_id=client_order_id,
                        exchange_order_id=eoid,
                        exchange_trade_id=tid_int,
                        fill_idempotency_key=key,
                        symbol=symbol,
                        side=side,
                        price=float(f.get("price", 0) or 0),
                        quantity=float(f.get("qty", 0) or 0),
                        quote_quantity=float(f.get("quoteQty", 0) or 0),
                        commission=ff.commission,
                        commission_asset=ff.commission_asset,
                        fee_quote=ff.fee_quote,
                        fee_valuation_status=ff.valuation_status,
                        trade_time=int(f.get("time", 0) or 0),
                    ))
                    existing.add(key)
                    added += 1
                await session.commit()
        except Exception:
            self.logger.exception("成交明细落库失败", symbol=symbol)
        return added

    async def _record_attempt(
        self,
        *,
        order_id: Optional[int],
        client_order_id: str,
        attempt_no: int,
        symbol: str,
        side: str,
        request: dict[str, Any],
        response: str,
        outcome: str,
        exchange_order_id: Optional[str] = None,
    ) -> None:
        """落一条执行尝试审计记录(V10.2, 尽力而为, 不阻断下单)"""
        import json as _json

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import ExecutionAttempt

        try:
            async with AsyncSessionLocal() as session:
                session.add(ExecutionAttempt(
                    order_id=order_id,
                    client_order_id=client_order_id,
                    attempt_no=attempt_no,
                    action="create",
                    symbol=symbol,
                    side=side,
                    request=_json.dumps(request, ensure_ascii=False, default=str)[:2000],
                    response=response[:2000],
                    outcome=outcome,
                    exchange_order_id=exchange_order_id,
                ))
                await session.commit()
        except Exception:
            self.logger.exception("执行尝试审计落库失败", client_order_id=client_order_id)

    # ---------- 策略绩效(V2.0) ----------

    async def _record_strategy_performance(self, signal: Signal, realized: float) -> None:
        """按策略累计绩效(strategy_performance 表, 供 AI 优化)

        realized 为本次成交的已实现盈亏(买入为 0), 累加而非读取累计值(避免重复累加)。
        """
        from sqlalchemy import select

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import StrategyPerformance

        # V9.0: 归因统一到组合策略伞(与 trade_records 的 source_strategy 口径一致)
        strategy_name = group_of(signal.source_strategy or signal.strategy)
        try:
            async with AsyncSessionLocal() as session:
                row = (
                    await session.execute(
                        select(StrategyPerformance).where(
                            StrategyPerformance.strategy == strategy_name,
                            StrategyPerformance.symbol == signal.symbol,
                        )
                    )
                ).scalar_one_or_none()
                if row is None:
                    row = StrategyPerformance(strategy=strategy_name, symbol=signal.symbol)
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

    # ---------- V10.6: 成交后本地记账(强一致事务) ----------

    def _accounting_lock(self, symbol: str) -> asyncio.Lock:
        """symbol 级记账互斥锁。

        并发同标的成交(如核心仓信号 / 多策略同时触发)会交错读写 Position 与
        FIFO lot 队列, 存在 add_buy 的 id 回填跨越 await 与 allocate_sell 消费
        尚未落库 lot 的竞态。串行化记账段消除该竞态(现货单币场景开销可忽略)。
        """
        lock = self._accounting_locks.get(symbol)
        if lock is None:
            lock = asyncio.Lock()
            self._accounting_locks[symbol] = lock
        return lock

    async def _apply_fill_accounting(
        self,
        signal: Signal,
        client_order_id: str,
        exchange_order_id: Optional[str],
        fill_qty: float,
        fill_price: float,
        fee: float,
        pos_before: float,
        cash_before: Optional[float],
        *,
        order_status: str = "FILLED",
        accounting_state: str = "OK",
    ) -> dict[str, Any]:
        """成交后本地记账: Position + PositionLot + SellAllocation + AccountLedger
        + Order 状态/记账态 在单个 DB 事务内提交。

        内存持仓/lot 先变更(权威态, 成交已发生), 本方法把镜像落库;
        任一写失败整体回滚(不留部分镜像), 异常上抛由调用方标记 RECOVERY_REQUIRED。
        V11.0(F4): 订单状态(FILLED/...)+ accounting_state 与记账同事务提交, 消除
        「状态已 FILLED 但记账未落」(崩溃后恢复引擎跳过 -> 永久漏记账)与「记账已落但
        RECOVERED 未落」(崩溃后重复记账)两个窗口。
        返回 {"realized", "pos", "pos_after"}。
        """
        from sqlalchemy import update

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import Order

        # 1. 内存持仓变更(权威态; portfolio 路径经 PortfolioEngine 记账)
        realized = 0.0
        if self.portfolio is not None:
            if signal.side.value == "BUY":
                self.portfolio.on_buy_fill(signal.symbol, fill_qty, fill_price, fee)
            else:
                realized, _ = self.portfolio.on_sell_fill(signal.symbol, fill_qty, fill_price, fee)
            pos = self.risk.positions.get(signal.symbol)
        else:
            if signal.side.value == "BUY":
                pos = self.risk.positions.apply_buy(signal.symbol, fill_qty, fill_price, fee)
            else:
                pos, realized = self.risk.positions.apply_sell(signal.symbol, fill_qty, fill_price, fee)

        pos_after = pos.quantity
        cash_after = self.paper.cash if self.is_paper else (
            cash_before + realized if cash_before is not None else None
        )

        fifo_realized = 0.0
        matched_cost = 0.0
        async with AsyncSessionLocal() as session:
            # Position 镜像
            await self.risk.positions.persist(signal.symbol, session=session)

            # Lot 会计(FIFO 批次 + 卖出分配)
            if signal.side.value == "BUY":
                await self.lot_tracker.add_buy(
                    signal.symbol, fill_qty, fill_price, fee,
                    client_order_id=client_order_id,
                    exchange_order_id=exchange_order_id,
                    session=session,
                )
            else:
                fifo_realized, matched_cost, _ = await self.lot_tracker.allocate_sell(
                    signal.symbol, fill_qty, fill_price, fee,
                    client_order_id=client_order_id,
                    exchange_order_id=exchange_order_id,
                    session=session,
                )

            # 审计账本(USDT + SOL 两行)
            if cash_before is not None and cash_after is not None:
                reason = f"{group_of(signal.source_strategy or signal.strategy)} {signal.reason_str[:400]}".strip()
                await self.account_ledger.record(
                    ts=int(time.time() * 1000),
                    symbol=signal.symbol,
                    bucket=getattr(signal, "bucket", "trade"),
                    side=signal.side.value,
                    cash_before=cash_before,
                    cash_after=cash_after,
                    pos_before=pos_before,
                    pos_after=pos_after,
                    reason=reason,
                    related_order_id=client_order_id,
                    commission=fee,
                    commission_asset="USDT" if fee else "",
                    realized_pnl=fifo_realized,
                    matched_cost=matched_cost,
                    session=session,
                )

            # V11.0(F4): 订单状态 + 成交数据 + 记账态 与记账同事务提交(崩溃窗口原子化)
            await session.execute(
                update(Order)
                .where(Order.client_order_id == client_order_id)
                .values(
                    status=order_status,
                    filled_quantity=fill_qty,
                    avg_fill_price=fill_price,
                    exchange_order_id=exchange_order_id or None,
                    accounting_state=accounting_state,
                )
            )

            await session.commit()

        return {"realized": realized, "pos": pos, "pos_after": pos_after}

    async def _mark_accounting_recovery_required(self, client_order_id: str) -> None:
        """本地记账事务失败: 置 Order.accounting_state=RECOVERY_REQUIRED 并急停冻结。

        交易所已成交但本地账本未落(整体回滚), 需要对账收敛修复;
        冻结(急停不自动复位)防止在账本漂移状态下继续交易。
        """
        from sqlalchemy import update

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import Order

        try:
            async with AsyncSessionLocal() as session:
                await session.execute(
                    update(Order)
                    .where(Order.client_order_id == client_order_id)
                    .values(accounting_state="RECOVERY_REQUIRED")
                )
                await session.commit()
        except Exception:
            self.logger.exception("标记 RECOVERY_REQUIRED 失败", client_order_id=client_order_id)

        self.risk.kill_switch.arm(f"本地记账失败 {client_order_id}")
        await self.risk.kill_switch.persist()

        await self.events.log(
            event_type="RECOVERY",
            client_order_id=client_order_id,
            source="recovery",
            payload={"accounting_state": "RECOVERY_REQUIRED"},
        )

    # ---------- V10.7: 订单恢复引擎(P0-c) ----------

    async def _set_accounting_state(self, client_order_id: str, state: str) -> None:
        """更新 Order.accounting_state(OK / RECOVERY_REQUIRED / RECOVERED), 尽力而为"""
        from sqlalchemy import update

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import Order

        try:
            async with AsyncSessionLocal() as session:
                await session.execute(
                    update(Order)
                    .where(Order.client_order_id == client_order_id)
                    .values(accounting_state=state)
                )
                await session.commit()
        except Exception:
            self.logger.exception("更新记账状态失败", client_order_id=client_order_id, state=state)

    def _find_lot(self, symbol: str, client_order_id: str) -> Optional[dict[str, Any]]:
        """在内存 FIFO lot 队列中按 client_order_id 定位 lot(进程内记账失败后仍存在)"""
        for lot in self.lot_tracker.lots.get(symbol, []):
            if lot.get("client_order_id") == client_order_id:
                return lot
        return None

    async def apply_recovered_fill(
        self,
        *,
        symbol: str,
        side: str,
        client_order_id: str,
        exchange_order_id: Optional[str],
        fill_qty: float,
        fill_price: float,
        fee: float = 0.0,
        final_status: str = "FILLED",
    ) -> str:
        """V10.7: 恢复路径成交记账(UNKNOWN/SUBMITTING 订单经交易所真相收敛为 FILLED 后调用)

        内存持仓从未变更(execute 在 UNKNOWN 分支提前返回、未记账), 故可安全复用
        _apply_fill_accounting 做完整记账(内存 + DB 镜像)。幂等: 仅当订单未记账
        (RECOVERED/正常成交终态已记账则跳过)时执行。

        V11.0(F4): 记账 + 订单状态(final_status) + accounting_state=RECOVERED 在同一
        事务提交, 消除「记账已落但 RECOVERED 未落」的崩溃后重复记账窗口; 同时把状态
        写也从独立的 _update_order_status 移入该事务, 消除「状态已 FILLED 但记账未落」
        的漏记账窗口。
        V11.0(F3): final_status 支持 "CANCELED" —— 交易所部分成交后撤/拒/过期时,
        记账部分成交并将订单终态落 CANCELED(而非 FILLED)。
        返回 filled / skip / error。
        """
        from sqlalchemy import select

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import Order
        from at50_strategy.strategy_base import Signal, SignalSide

        async with AsyncSessionLocal() as session:
            row = (await session.execute(
                select(Order).where(Order.client_order_id == client_order_id)
            )).scalar_one_or_none()
        if row is None:
            return "skip"
        if row.accounting_state == "RECOVERED":
            return "skip"  # 已恢复(幂等)
        if row.accounting_state == "OK" and row.status in ("FILLED", "PARTIALLY_FILLED"):
            return "skip"  # 正常路径已记账(终态成交 + OK), 不重复
        if row.status in ("CANCELED", "REJECTED", "EXPIRED"):
            return "skip"  # 无成交可记账(终端无成交态)

        # 成交明细落库(幂等), 真实均价/手续费优先; 失败回退到订单级成交数据
        if self.rest is not None:
            try:
                metrics = await self._ingest_fills(
                    order_id=row.id, client_order_id=client_order_id,
                    exchange_order_id=exchange_order_id, symbol=symbol, side=side,
                )
                if metrics is not None:
                    fill_price, fee, _ = metrics
            except Exception:
                self.logger.warning(
                    "恢复路径成交明细摄入失败, 用订单级成交数据",
                    client_order_id=client_order_id,
                )

        signal = Signal(
            symbol=symbol, strategy="recovery", side=SignalSide(side),
            price=fill_price, quantity=fill_qty, reason=["recovery"],
        )
        pos_before = self.risk.positions.get(symbol).quantity
        cash_before = self.paper.cash if self.is_paper else None
        try:
            async with self._accounting_lock(symbol):
                acct = await self._apply_fill_accounting(
                    signal, client_order_id, exchange_order_id,
                    fill_qty, fill_price, fee, pos_before, cash_before,
                    order_status=final_status, accounting_state="RECOVERED",
                )
        except Exception:
            self.logger.exception(
                "恢复路径成交记账失败", symbol=symbol, client_order_id=client_order_id,
            )
            await self._mark_accounting_recovery_required(client_order_id)
            return "error"

        remaining = acct["pos_after"]
        self.trade_sm.on_order_filled(symbol, side, remaining)
        await self.trade_sm.persist(symbol)
        await self.events.log(
            event_type="RECOVERED", client_order_id=client_order_id,
            exchange_order_id=exchange_order_id, source="recovery",
            payload={"side": side, "fill_qty": fill_qty, "fill_price": fill_price},
        )
        return "filled"

    async def _rebuild_buy_mirror(
        self, symbol: str, client_order_id: str, lot: dict[str, Any]
    ) -> str:
        """进程内 BUY 记账失败: 内存已含该 lot(权威态), 仅补 DB 镜像(PositionLot + Position)。

        不重复变更内存(避免重复记账); 幂等: 若 DB 已有同 client_order_id 的 PositionLot,
        只回填 lot id 不重复插入。返回 recovered / error。
        """
        from sqlalchemy import select

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import PositionLot

        try:
            async with AsyncSessionLocal() as session:
                existing = (await session.execute(
                    select(PositionLot).where(PositionLot.client_order_id == client_order_id)
                )).scalars().all()
                if existing:
                    lot["id"] = existing[0].id
                else:
                    row = PositionLot(
                        symbol=symbol,
                        quantity=lot.get("quantity", 0.0),
                        price=lot.get("price", 0.0),
                        fee_quote=lot.get("fee_quote", 0.0),
                        client_order_id=client_order_id,
                        exchange_order_id=lot.get("exchange_order_id"),
                        status="open",
                    )
                    session.add(row)
                    await session.flush()
                    lot["id"] = row.id
                    await session.commit()
        except Exception:
            self.logger.exception(
                "BUY 账务镜像重建(PositionLot)失败", client_order_id=client_order_id,
            )
            return "error"

        try:
            await self.risk.positions.persist(symbol)
        except Exception:
            self.logger.exception(
                "BUY 账务镜像重建(Position)失败", client_order_id=client_order_id,
            )
            return "error"

        await self._set_accounting_state(client_order_id, "RECOVERED")
        await self.events.log(
            event_type="RECOVERED", client_order_id=client_order_id, source="recovery",
        )
        return "recovered"

    async def rebuild_buy_accounting(
        self,
        *,
        symbol: str,
        client_order_id: str,
        exchange_order_id: Optional[str] = None,
        fill_qty: float,
        fill_price: float,
        fee: float = 0.0,
    ) -> str:
        """V10.7: RECOVERY_REQUIRED BUY 账务重建。

        - 内存已含该 lot(进程内失败, 权威态未丢): 仅补 DB 镜像(不重复记账);
        - 内存无该 lot(重启后, 权威态丢失): 走完整记账 apply_recovered_fill(内存 + DB)。
        完成后置 accounting_state=RECOVERED。返回 recovered / filled / skip / error。
        """
        lot = self._find_lot(symbol, client_order_id)
        if lot is not None:
            return await self._rebuild_buy_mirror(symbol, client_order_id, lot)
        return await self.apply_recovered_fill(
            symbol=symbol, side="BUY", client_order_id=client_order_id,
            exchange_order_id=exchange_order_id, fill_qty=fill_qty,
            fill_price=fill_price, fee=fee,
        )

    async def rebuild_sell_accounting(
        self,
        *,
        symbol: str,
        client_order_id: str,
        exchange_order_id: Optional[str] = None,
        fill_qty: float,
        fill_price: float,
        fee: float = 0.0,
    ) -> str:
        """V11.1(P0-4): RECOVERY_REQUIRED SELL 账务重建(消除人工冻结)。

        SELL 与 BUY 不同: 记账失败时 in-memory lot 已被 allocate_sell 消费
        (内存先改、DB 事务整体回滚), 故不能复用 apply_recovered_fill —— 它会对
        已分歧的内存二次消费。这里从 DB 开仓 PositionLot(权威未消费态)确定性重放
        FIFO 分配, 单事务落 SellAllocation + 减 lot + 更新 Position(平均成本口径) +
        Order, 完成后重同步内存 lot/持仓(权威态 = DB)。两场景(进程内失败 / 重启后)
        收敛一致: DB 始终是回滚后的「卖出前」权威快照。

        幂等: accounting_state=RECOVERED / 终态成交已记账(OK+FILLED) / 无成交终态
        则 skip。返回 filled / skip / error。
        """
        from sqlalchemy import select, update

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import Order, Position, PositionLot, SellAllocation

        # 幂等守卫(与 apply_recovered_fill 一致)
        async with AsyncSessionLocal() as session:
            row = (await session.execute(
                select(Order).where(Order.client_order_id == client_order_id)
            )).scalar_one_or_none()
        if row is None:
            return "skip"
        if row.accounting_state == "RECOVERED":
            return "skip"  # 已恢复(幂等)
        if row.accounting_state == "OK" and row.status in ("FILLED", "PARTIALLY_FILLED"):
            return "skip"  # 正常路径已记账, 不重复
        if row.status in ("CANCELED", "REJECTED", "EXPIRED"):
            return "skip"  # 无成交可记账

        # 真实手续费/成交价(尽力而为; 失败回退到订单级成交数据)
        if self.rest is not None and exchange_order_id:
            try:
                metrics = await self._ingest_fills(
                    order_id=row.id, client_order_id=client_order_id,
                    exchange_order_id=exchange_order_id, symbol=symbol, side="SELL",
                )
                if metrics is not None:
                    fill_price, fee, _ = metrics
            except Exception:
                self.logger.warning(
                    "SELL 恢复成交明细摄入失败, 用订单级成交数据",
                    client_order_id=client_order_id,
                )

        # 读 DB 开仓 lot(权威未消费态)与持仓(平均成本口径快照)
        async with AsyncSessionLocal() as session:
            lots = (await session.execute(
                select(PositionLot).where(
                    PositionLot.symbol == symbol, PositionLot.status == "open"
                ).order_by(PositionLot.id)
            )).scalars().all()
            pos_row = (await session.execute(
                select(Position).where(Position.symbol == symbol)
            )).scalar_one_or_none()

        pos_qty0 = pos_row.quantity if pos_row is not None else 0.0
        pos_avg0 = pos_row.avg_price if pos_row is not None else 0.0
        pos_realized0 = pos_row.realized_pnl if pos_row is not None else 0.0
        pos_peak0 = pos_row.peak_price if pos_row is not None else 0.0

        # FIFO 分配(纯计算, 不触碰内存 lot; 与 allocate_sell 同口径)
        allocations: list[dict[str, Any]] = []
        new_quantities: dict[int, float] = {}
        remaining = fill_qty
        matched_qty = 0.0
        for lot in lots:
            if remaining <= _SELL_EPS:
                new_quantities.setdefault(lot.id, lot.quantity)
                continue
            if lot.quantity <= _SELL_EPS:
                new_quantities.setdefault(lot.id, 0.0)
                continue
            alloc_qty = min(remaining, lot.quantity)
            allocations.append({
                "lot_id": lot.id,
                "quantity": alloc_qty,
                "lot_price": lot.price,
                "sell_price": fill_price,
                "realized_pnl": (fill_price - lot.price) * alloc_qty,
            })
            new_quantities[lot.id] = lot.quantity - alloc_qty
            matched_qty += alloc_qty
            remaining -= alloc_qty

        if remaining > _SELL_EPS:
            self.logger.warning(
                "SELL 重建卖出量超开仓 lot, 按可卖量截断",
                symbol=symbol, fill_qty=fill_qty, matched=matched_qty,
            )

        # 平均成本口径已实现盈亏(与 positions.apply_sell 一致: 卖出不改 avg_price)
        if matched_qty > _SELL_EPS and pos_avg0 > 0:
            avg_realized = (fill_price - pos_avg0) * matched_qty - fee
        else:
            avg_realized = -fee
        new_qty = max(0.0, pos_qty0 - matched_qty)
        new_avg = pos_avg0 if new_qty > _SELL_EPS else 0.0
        new_realized = pos_realized0 + avg_realized
        new_peak = pos_peak0 if new_qty > _SELL_EPS else 0.0

        try:
            async with self._accounting_lock(symbol):
                async with AsyncSessionLocal() as session:
                    for a in allocations:
                        session.add(SellAllocation(
                            symbol=symbol,
                            sell_client_order_id=client_order_id,
                            sell_exchange_order_id=exchange_order_id,
                            lot_id=a["lot_id"],
                            quantity=a["quantity"],
                            lot_price=a["lot_price"],
                            sell_price=a["sell_price"],
                            realized_pnl=a["realized_pnl"],
                        ))
                    for lid, qty in new_quantities.items():
                        await session.execute(
                            update(PositionLot).where(PositionLot.id == lid).values(
                                quantity=qty,
                                status="closed" if qty <= _SELL_EPS else "open",
                            )
                        )
                    # Position 镜像(平均成本口径)
                    if pos_row is None:
                        session.add(Position(
                            symbol=symbol, quantity=new_qty, avg_price=new_avg,
                            realized_pnl=new_realized, peak_price=new_peak,
                        ))
                    else:
                        await session.execute(
                            update(Position).where(Position.symbol == symbol).values(
                                quantity=new_qty, avg_price=new_avg,
                                realized_pnl=new_realized, peak_price=new_peak,
                            )
                        )
                    # Order 状态 + 成交数据 + 记账态 同事务(与 _apply_fill_accounting 一致)
                    await session.execute(
                        update(Order).where(Order.client_order_id == client_order_id).values(
                            status="FILLED", filled_quantity=fill_qty,
                            avg_fill_price=fill_price,
                            exchange_order_id=exchange_order_id or None,
                            accounting_state="RECOVERED",
                        )
                    )
                    await session.commit()
        except Exception:
            self.logger.exception(
                "SELL 账务重建失败", symbol=symbol, client_order_id=client_order_id,
            )
            await self._mark_accounting_recovery_required(client_order_id)
            return "error"

        # 重同步内存持仓 + FIFO lot 队列(权威态 = DB)
        self._resync_sell_memory(
            symbol, new_qty, new_avg, new_realized, new_peak, lots, new_quantities,
        )

        self.trade_sm.on_order_filled(symbol, "SELL", new_qty)
        await self.trade_sm.persist(symbol)
        await self.events.log(
            event_type="RECOVERED", client_order_id=client_order_id,
            exchange_order_id=exchange_order_id, source="recovery",
            payload={"side": "SELL", "fill_qty": fill_qty, "fill_price": fill_price},
        )
        return "filled"

    def _resync_sell_memory(
        self,
        symbol: str,
        new_qty: float,
        new_avg: float,
        new_realized: float,
        new_peak: float,
        lots: list[Any],
        new_quantities: dict[int, float],
    ) -> None:
        """SELL 重建后重同步内存持仓(PositionState)与 FIFO lot 队列(权威态 = DB)。

        覆盖进程内失败遗留的「已消费」分歧内存(内存先改、DB 回滚), 使其与 DB 收敛。
        """
        pos = self.risk.positions.get(symbol)
        pos.quantity = new_qty
        pos.avg_price = new_avg
        pos.realized_pnl = new_realized
        pos.peak_price = new_peak
        if new_qty <= _SELL_EPS:
            pos.entry_ts = 0.0
            pos.trough_price = 0.0

        open_lots: list[dict[str, Any]] = []
        for lot in lots:
            qty = new_quantities.get(lot.id, lot.quantity)
            if qty <= _SELL_EPS:
                continue
            open_lots.append({
                "id": lot.id,
                "symbol": symbol,
                "quantity": qty,
                "price": lot.price,
                "fee_quote": lot.fee_quote,
                "client_order_id": lot.client_order_id,
                "exchange_order_id": lot.exchange_order_id,
            })
        self.lot_tracker.lots[symbol] = open_lots

    # ---------- V10.7: 订单事件日志 ----------

    @staticmethod
    def _status_event(status: str) -> str:
        """订单状态 -> 事件类型(FILLED -> FILL, PARTIALLY_FILLED -> PARTIAL_FILL 等)"""
        return {
            "FILLED": "FILL",
            "PARTIALLY_FILLED": "PARTIAL_FILL",
            "CANCELED": "CANCELED",
            "REJECTED": "REJECTED",
            "EXPIRED": "EXPIRED",
            "NEW": "ORDER_CREATED",
            "SUBMITTING": "SUBMITTING",
            "UNKNOWN": "UNKNOWN",
        }.get(status, status)

    async def _log_order_event(
        self,
        client_order_id: str,
        status: str,
        exchange_order_id: Optional[str] = None,
        order_id: Optional[int] = None,
    ) -> None:
        """订单状态跃迁 -> 落一条执行事件(append-only, 尽力而为)"""
        await self.events.log(
            event_type=self._status_event(status),
            client_order_id=client_order_id,
            exchange_order_id=exchange_order_id,
            order_id=order_id,
            payload={"status": status},
        )

    # ---------- 订单记录 ----------

    async def _create_order_record(
        self, signal: Signal, client_order_id: str, exec_qty: float
    ) -> int | None:
        """落库新订单(V2.0: 含原因),返回订单主键

        signal.quantity 落 signals 表(原始意图), exec_qty 落 orders 表(实际提交数量, 二者分离)。
        """
        import json as _json

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import Order, Signal as SignalModel

        try:
            async with AsyncSessionLocal() as session:
                # 同步信号状态(原始意图数量, 不改写)
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
                    quantity=exec_qty,
                    status="NEW",
                    strategy=signal.strategy,
                    signal_id=sig_row.id,
                    is_paper=self.is_paper,
                    reduce_only=signal.side.value == "SELL",
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

    # ---------- V10: 急停撤单 ----------

    async def cancel_all_open_orders(self, symbol: str) -> int:
        """撤销所有未成交订单(live 撤交易所挂单, paper 撤本地 NEW 单), 返回撤单数

        急停时调用: 冻结闸门已生效, 本方法负责清掉已挂出但未成交的订单,
        并把本地订单表非终态行改为 CANCELED、状态机回退。
        """
        from sqlalchemy import update

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import Order

        canceled = 0
        if self.is_paper:
            for oid in list(self.paper.orders.keys()):
                order = self.paper.orders[oid]
                if order.symbol == symbol and order.status == "NEW":
                    if await self.paper.cancel_order(oid):
                        canceled += 1
                        self.trade_sm.on_order_canceled(
                            symbol, order.side, self.risk.positions.get(symbol).quantity
                        )
            if canceled:
                try:
                    async with AsyncSessionLocal() as session:
                        await session.execute(
                            update(Order)
                            .where(Order.symbol == symbol, Order.is_paper.is_(True), Order.status == "NEW")
                            .values(status="CANCELED")
                        )
                        await session.commit()
                except Exception:
                    self.logger.exception("纸面撤单落库失败")
        else:
            if self.rest is None:
                return 0
            try:
                open_orders = await self.rest.get_open_orders(symbol)
            except Exception as e:
                self.logger.warning("急停撤单获取挂单失败", error=str(e))
                return 0
            for o in open_orders:
                oid = str(o.get("orderId", ""))
                side = str(o.get("side", "")).upper()
                try:
                    await self.rest.cancel_order(symbol, oid)
                    canceled += 1
                    self.trade_sm.on_order_canceled(
                        symbol, side, self.risk.positions.get(symbol).quantity
                    )
                except Exception:
                    self.logger.warning("急停撤单失败", order_id=oid)
            if canceled:
                try:
                    async with AsyncSessionLocal() as session:
                        await session.execute(
                            update(Order)
                            .where(
                                Order.symbol == symbol,
                                Order.is_paper.is_(False),
                                Order.status.in_(("NEW", "PARTIALLY_FILLED")),
                            )
                            .values(status="CANCELED")
                        )
                        await session.commit()
                except Exception:
                    self.logger.exception("实盘撤单落库失败")

        if canceled:
            await self.trade_sm.persist(symbol)
        self.logger.info("急停撤单完成", symbol=symbol, canceled=canceled, paper=self.is_paper)
        return canceled

    async def close(self) -> None:
        """清理"""
        return
