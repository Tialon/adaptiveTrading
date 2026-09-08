"""
风控管理器(V2.0)

增强:
1. 百分比风控: 持仓 ≤ 权益40%, 单笔 ≤ 权益5%, 日亏 5%, 回撤 15%
2. 异常保护:
   - 价格瞬间波动(单笔 tick 偏离 >3%) -> 暂停交易
   - 行情静默(WS 超时) -> 暂停交易
   - 执行连续失败 -> 暂停交易
3. 观察档信号拒绝(策略标注 observe 未达买入阈值)

审批链: 信号 -> 价格有效 -> 异常保护 -> 熔断 -> 单笔限额 -> 仓位限额 -> 定价(数量)
"""

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Optional

from at01_common.settings import get_settings
from at01_common.logger import LoggerMixin
from at60_risk.risk_breaker import CircuitBreaker
from at60_risk.risk_drawdown import DrawdownController
from at60_risk.risk_killswitch import KillSwitch
from at60_risk.risk_position import PositionManager
from at60_risk.risk_state import RiskStateMachine
from at50_strategy.strategy_base import Signal, SignalSide

MIN_NOTIONAL = 10.0  # 最小名义价值


@dataclass
class RiskDecision:
    """风控决策"""

    approved: bool
    reason: str = ""
    quantity: float = 0.0  # 批准的数量
    price: float = 0.0
    observed: bool = False  # 观察档(未达执行阈值)

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "reason": self.reason,
            "quantity": self.quantity,
            "price": self.price,
            "observed": self.observed,
        }


class RiskManager(LoggerMixin):
    """风控管理器"""

    def __init__(
        self,
        positions: Optional[PositionManager] = None,
        drawdown: Optional[DrawdownController] = None,
        breaker: Optional[CircuitBreaker] = None,
    ):
        self.settings = get_settings()
        self.positions = positions or PositionManager()
        self.drawdown = drawdown or DrawdownController()
        self.breaker = breaker or CircuitBreaker()
        self.kill_switch = KillSwitch()  # V10: 持久化急停开关(不自动复位)
        self.reject_count = 0
        self.approve_count = 0
        self.observe_count = 0

        # V10.5 显式风险状态机(取代隐式时间阈值暂停)
        self.state_machine = RiskStateMachine(
            pause_seconds=self.settings.risk_anomaly_pause_seconds
        )
        self._silence_active: bool = False  # 静默状态标记
        self._last_tick_price: dict[str, float] = {}
        self._last_tick_time: float = 0.0
        self._consecutive_errors: int = 0
        # V8: 快速暴跌检测(短窗口价格历史)
        self._price_history: dict[str, deque] = {}
        self._fast_crash_window: float = 900.0  # 15 分钟
        self._fast_crash_pct: float = 0.10  # 10% 跌幅

        # V11.3 P0-7: 追踪 fire-and-forget 风险事件任务(防泄漏 / 防 GC / 便于停机等待)
        self._pending_tasks: set[asyncio.Task] = set()

    # ---------- 限额计算(百分比) ----------

    @property
    def max_position_quote(self) -> float:
        """最大持仓金额"""
        if self.settings.risk_max_position_quote > 0:  # V1 兼容
            return self.settings.risk_max_position_quote
        return self.current_equity * self.settings.risk_max_position_pct

    @property
    def max_single_order_quote(self) -> float:
        """单笔最大金额"""
        if self.settings.risk_max_single_order_quote > 0:  # V1 兼容
            return self.settings.risk_max_single_order_quote
        return self.current_equity * self.settings.risk_max_single_order_pct

    @property
    def max_sol_exposure_quote(self) -> float:
        """V12 §16: SOL 总敞口硬上限(金额) = 权益 × risk_max_sol_exposure。

        区别于 max_position_quote(单仓/策略信号子仓上限): 本值是「全部 SOL 市值 / 权益」
        的硬天花板, 覆盖核心仓 + 交易仓两条路径, 超限只禁买、放行卖。
        """
        return self.current_equity * self.settings.risk_max_sol_exposure

    def sol_exposure_ratio(self, last_prices: dict[str, float]) -> float:
        """V12 §16: 当前 SOL 总市值 / 权益(敞口比)。权益 <= 0 返回 0。"""
        equity = self.equity(last_prices)
        if equity <= 0:
            return 0.0
        return self.positions.total_position_quote(last_prices) / equity

    @property
    def current_equity(self) -> float:
        """当前权益(缓存的最新值)"""
        return self.breaker.current_equity or self.settings.risk_initial_equity

    # ---------- 权益 ----------

    def equity(self, last_prices: dict[str, float]) -> float:
        """总权益 = 初始 + 已实现盈亏 + 未实现盈亏"""
        realized = sum(p.realized_pnl for p in self.positions.positions.values())
        unrealized = sum(
            self.positions.unrealized_pnl(symbol, price)
            for symbol, price in last_prices.items()
        )
        return self.settings.risk_initial_equity + realized + unrealized

    def update_equity(self, last_prices: dict[str, float]) -> dict[str, Any]:
        """每轮更新权益/回撤/日内亏损, 必要时触发风险状态迁移。

        V12 §18-19 语义修正:
        - 日内亏损 ≥ risk_max_daily_loss(3%) → REDUCE_ONLY(禁开新仓、保留卖出),
          而非「熔断冷却」或「自动清仓」。
        - 回撤 ≥ risk_max_drawdown(15%) → 持久急停(KILL, 需人工检查),
          而非「冷却自动复位」的 CircuitBreaker 熔断。
        """
        eq = self.equity(last_prices)
        self.breaker.current_equity = eq

        dd, dd_breach = self.drawdown.update(eq)

        # V12 §19: 15% 回撤 → 急停冻结(KILL, 不自动恢复)
        if dd_breach:
            self.kill_switch.arm(
                f"最大回撤 {dd:.2%} >= {self.settings.risk_max_drawdown:.2%}"
            )
            self.state_machine.kill(f"最大回撤 {dd:.2%}")
            self._record_event_now("drawdown", detail=f"回撤{dd:.2%} 触发急停", equity=eq)

        # V12 §18: 日内亏损 3% → 仅减仓(禁开新仓、保留卖出)
        daily_ratio = self.breaker.daily_loss_ratio(eq)
        if daily_ratio <= -self.settings.risk_max_daily_loss:
            self.reduce_only(
                f"日内亏损超限(>{self.settings.risk_max_daily_loss:.0%})"
            )

        return {"equity": eq, "drawdown": dd, "breaker_open": self.breaker.is_open}

    # ---------- 异常保护(V2.0) ----------

    def check_tick_anomaly(self, symbol: str, price: float) -> bool:
        """价格瞬间波动检测: 单笔偏离超阈值 -> 暂停交易"""
        last = self._last_tick_price.get(symbol)
        self._last_tick_price[symbol] = price
        self._last_tick_time = time.time()
        if last is None or last <= 0:
            return False
        change = abs(price - last) / last
        if change >= self.settings.risk_price_spike_pct:
            self._pause(f"价格瞬间波动{change:.1%}({symbol}: {last:.2f}->{price:.2f})")
            self._record_event_now(
                "anomaly", detail=f"价格异常 {symbol} {change:.2%}", equity=self.current_equity
            )
            return True
        return False

    def check_fast_crash(self, symbol: str, price: float) -> bool:
        """快速暴跌检测: 短窗口(默认 15 分钟)内跌幅超阈值(10%) -> 暂停交易

        闪崩保护: 捕获突发式抛售(与单笔 tick 尖刺互补)。
        """
        now = time.time()
        hist = self._price_history.setdefault(symbol, deque())
        hist.append((now, price))
        cutoff = now - self._fast_crash_window
        while hist and hist[0][0] < cutoff:
            hist.popleft()
        if len(hist) < 2 or hist[0][1] <= 0:
            return False
        base = hist[0][1]
        if price < base * (1 - self._fast_crash_pct):
            drop = 1 - price / base
            self._pause(f"快速暴跌 {symbol} 窗口内跌幅{drop:.1%}")
            self._record_event_now(
                "anomaly", detail=f"快速暴跌 {symbol} {drop:.1%}", equity=self.current_equity
            )
            return True
        return False

    def check_market_silence(self) -> bool:
        """行情静默检测: 超过阈值无 tick -> 暂停(仅状态切换时告警, 不重复刷屏)"""
        if self._last_tick_time <= 0:
            return False
        silent_for = time.time() - self._last_tick_time
        if silent_for > self.settings.risk_max_ws_silence_seconds:
            if not self._silence_active:
                self._silence_active = True  # 首次进入静默
                self._pause(f"行情静默{silent_for:.0f}秒")
            return True
        # 行情恢复: 重置静默标记
        if self._silence_active:
            self._silence_active = False
            self.logger.info("行情静默解除", silent_for=f"{silent_for:.0f}秒内恢复")
        return False

    @property
    def ws_silence_seconds(self) -> float:
        """距最近 tick 的秒数(从未收到 tick 返回 0)。供可观测性 data_gap 指标采集。"""
        if self._last_tick_time <= 0:
            return 0.0
        return time.time() - self._last_tick_time

    @property
    def silence_active(self) -> bool:
        """行情静默是否正在生效(供 run.py 在进入静默的瞬间 +1 data_gaps 计数)。"""
        return self._silence_active

    def record_execution_error(self) -> None:
        """执行失败计数,连续 3 次暂停"""
        self._consecutive_errors += 1
        if self._consecutive_errors >= 3:
            self._pause(f"连续执行失败{self._consecutive_errors}次")

    def record_execution_success(self) -> None:
        self._consecutive_errors = 0

    @property
    def anomaly_paused(self) -> bool:
        """异常暂停是否生效(状态机读前自动结算时间窗到期)"""
        return self.state_machine.is_paused()

    def _pause(self, reason: str) -> None:
        """触发交易暂停(同原因持续期间只告警一次, 延长冷却静默)"""
        if self.state_machine.pause(reason):
            # 状态切换或原因变化: 告警 + 落审计事件
            self.logger.error(
                "交易暂停(异常保护)", reason=reason,
                seconds=self.settings.risk_anomaly_pause_seconds,
            )
            self._record_event_now(
                "risk_state", detail=f"PAUSED: {reason}", equity=self.current_equity
            )

    def pause(self, reason: str) -> None:
        """公开的交易暂停入口(供对账/数据校验等外部模块触发)"""
        self._pause(reason)

    # ---------- 统一交易闸门(V9.0) ----------

    def can_trade(self) -> bool:
        """统一交易闸门: 急停 / 熔断 / 异常保护(含静默)任一触发即禁止开新仓

        供 _on_signal / 核心仓决策 / 审批链复用, 短路一切新交易。
        """
        if self.kill_switch.is_armed:
            return False
        if self.breaker.is_open:
            return False
        if not self.state_machine.can_trade():
            return False
        return True

    def can_buy(self) -> bool:
        """买入闸门(开新仓): 急停/熔断/风险状态(NORMAL 才可买)任一触发即禁"""
        if self.kill_switch.is_armed:
            return False
        if self.breaker.is_open:
            return False
        if not self.state_machine.can_buy():
            return False
        return True

    def can_sell(self) -> bool:
        """卖出闸门(减仓): 急停/熔断仍禁; 风险状态 NORMAL/REDUCE_ONLY 可卖"""
        if self.kill_switch.is_armed:
            return False
        if self.breaker.is_open:
            return False
        if not self.state_machine.can_sell():
            return False
        return True

    def reduce_only(self, reason: str) -> None:
        """进入仅减仓态(禁开新仓、保留卖出), 状态切换时告警 + 落审计事件"""
        if self.state_machine.reduce_only(reason):
            self.logger.error("进入仅减仓(禁开新仓)", reason=reason)
            self._record_event_now(
                "risk_state", detail=f"REDUCE_ONLY: {reason}", equity=self.current_equity
            )

    @property
    def block_reason(self) -> str:
        """当前被闸门拦截的原因(空串=可交易)"""
        if self.kill_switch.is_armed:
            return f"急停中: {self.kill_switch.reason}"
        if self.breaker.is_open:
            return f"熔断中: {self.breaker.reason}"
        if self.state_machine.is_recovery_check():
            return f"恢复核验中: {self.state_machine.reason}"
        if self.state_machine.is_paused():
            return f"异常保护: {self.state_machine.reason}"
        if self.state_machine.is_reduce_only():
            return f"仅减仓: {self.state_machine.reason}"
        return ""

    def _record_event_now(self, event_type: str, detail: str, equity: Optional[float] = None) -> None:
        """同步上下文记录风控事件(调度到事件循环, 不阻塞)。

        V11.3 P0-7: 任务被 `self._pending_tasks` 追踪 + done 回调自动移除, 既不泄漏
        也不被提前 GC(此前 `loop.create_task(...)` 结果被丢弃, 属未追踪任务)。
        """
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(self._record_event(event_type, detail, equity))
            self._pending_tasks.add(task)
            task.add_done_callback(self._pending_tasks.discard)
        except RuntimeError:
            pass

    async def flush_events(self) -> None:
        """等待所有在途风险事件落库(停机前调用, 防丢失审计事件)。

        用 gather 并发等待全部在途任务, 结束后显式 clear: done 回调经 `call_soon`
        调度、可能尚未运行(尤其多任务近同时完成时), 仅靠 `await task` 无法保证
        返回时集合已清空, 会造成「停机后仍残留已完成任务」的假泄漏。
        """
        pending = list(self._pending_tasks)
        if not pending:
            return
        results = await asyncio.gather(*pending, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                self.logger.error("风险事件落库异常(停机 flush)", error=str(result))
        self._pending_tasks.clear()

    # ---------- 审批 ----------

    async def check(self, signal: Signal, last_prices: dict[str, float]) -> RiskDecision:
        """信号风控审批"""
        symbol = signal.symbol
        price = signal.price

        # 0. 观察档(策略标注未达执行阈值)
        if signal.reason and "观察档" in signal.reason_str:
            self.observe_count += 1
            return RiskDecision(approved=False, reason="观察档信号不执行", observed=True)

        # 1. 方向闸门(急停/熔断/风险状态): 买看 can_buy, 卖看 can_sell(仅减仓态放行卖出)
        if signal.side == SignalSide.BUY and not self.can_buy():
            return self._reject(signal, self.block_reason)
        if signal.side == SignalSide.SELL and not self.can_sell():
            return self._reject(signal, self.block_reason)

        # 3. 价格有效性
        if price <= 0:
            return self._reject(signal, "价格无效")

        # 4. 目标数量
        if signal.quantity:
            quantity = signal.quantity
        elif signal.quote_amount:
            quantity = signal.quote_amount / price
        else:
            return self._reject(signal, "信号缺少数量/金额")

        # 5. 单笔金额限制(百分比)
        order_quote = quantity * price
        if order_quote > self.max_single_order_quote:
            quantity = self.max_single_order_quote / price
            order_quote = self.max_single_order_quote
            self.logger.warning("单笔金额超限,已缩量", symbol=symbol, capped=order_quote)

        if order_quote < MIN_NOTIONAL:
            return self._reject(signal, f"订单金额过小 {order_quote:.2f} USDT")

        if signal.side == SignalSide.BUY:
            return await self._check_buy(signal, quantity, price, last_prices)
        return await self._check_sell(signal, quantity, price, last_prices)

    async def _check_buy(
        self, signal: Signal, quantity: float, price: float, last_prices: dict[str, float]
    ) -> RiskDecision:
        """买入审批: 持仓限额(权益百分比) + SOL 总敞口硬上限(V12 §16)"""
        symbol = signal.symbol
        pos = self.positions.get(symbol)
        current_quote = pos.quantity * (last_prices.get(symbol, price))
        new_quote = current_quote + quantity * price

        if new_quote > self.max_position_quote:
            room = self.max_position_quote - current_quote
            if room < MIN_NOTIONAL:
                return self._reject(
                    signal,
                    f"持仓超限: 当前{current_quote:.0f} + 新增{quantity*price:.0f} > "
                    f"限额{self.max_position_quote:.0f}({self.settings.risk_max_position_pct:.0%}权益)",
                )
            # 缩量至剩余额度
            quantity = room / price
            self.logger.warning("买入缩量至持仓限额内", symbol=symbol, new_qty=quantity)

        # V12 §16: SOL 总敞口硬上限(SOL 市值/权益 ≤ risk_max_sol_exposure, 超限禁买)
        equity = self.equity(last_prices)
        sol_value = self.positions.total_position_quote(last_prices)
        exposure_cap = equity * self.settings.risk_max_sol_exposure
        new_sol_value = sol_value + quantity * price
        if new_sol_value > exposure_cap:
            room = exposure_cap - sol_value
            if room < MIN_NOTIONAL:
                return self._reject(
                    signal,
                    f"SOL 敞口超限: 买入后敞口 {new_sol_value:.0f} > 上限 {exposure_cap:.0f}"
                    f"({self.settings.risk_max_sol_exposure:.0%}权益)",
                )
            # 缩量至敞口硬上限内
            quantity = room / price
            self.logger.warning("买入缩量至 SOL 敞口硬上限内", symbol=symbol, new_qty=quantity)

        return self._approve(signal, quantity, price)

    async def _check_sell(
        self, signal: Signal, quantity: float, price: float, last_prices: dict[str, float]
    ) -> RiskDecision:
        """卖出审批: 不能卖出超过持仓"""
        symbol = signal.symbol
        pos = self.positions.get(symbol)
        if pos.quantity <= 0:
            return self._reject(signal, "无持仓,忽略卖出信号")

        quantity = min(quantity, pos.quantity)
        return self._approve(signal, quantity, price)

    # ---------- 内部 ----------

    def _approve(self, signal: Signal, quantity: float, price: float) -> RiskDecision:
        self.approve_count += 1
        self.logger.info(
            "风控批准", symbol=signal.symbol, side=signal.side.value,
            qty=quantity, price=price,
        )
        return RiskDecision(approved=True, quantity=quantity, price=price)

    def _reject(self, signal: Signal, reason: str) -> RiskDecision:
        self.reject_count += 1
        self.logger.info("风控拒绝", symbol=signal.symbol, side=signal.side.value, reason=reason)
        return RiskDecision(approved=False, reason=reason)

    async def _record_event(self, event_type: str, detail: str, equity: Optional[float] = None) -> None:
        """记录风控事件"""
        try:
            from at01_common.database import AsyncSessionLocal
            from at01_common.models import RiskEvent

            async with AsyncSessionLocal() as session:
                session.add(RiskEvent(event_type=event_type, detail=detail, equity=equity))
                await session.commit()
        except Exception:
            self.logger.exception("风控事件持久化失败")

    def status(self) -> dict[str, Any]:
        return {
            "approve_count": self.approve_count,
            "reject_count": self.reject_count,
            "observe_count": self.observe_count,
            "equity": round(self.current_equity, 2),
            "max_position_quote": round(self.max_position_quote, 2),
            "max_single_order_quote": round(self.max_single_order_quote, 2),
            "anomaly_paused": self.anomaly_paused,
            "anomaly_reason": self.state_machine.reason,
            "risk_state": self.state_machine.status(),
            "drawdown": self.drawdown.status(),
            "breaker": self.breaker.status(),
            "kill_switch": self.kill_switch.status(),
            "positions": {s: p.to_dict() for s, p in self.positions.positions.items()},
        }
