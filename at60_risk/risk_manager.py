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

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from at01_common.settings import get_settings
from at01_common.logger import LoggerMixin
from at60_risk.risk_breaker import CircuitBreaker
from at60_risk.risk_drawdown import DrawdownController
from at60_risk.risk_position import PositionManager
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
        self.reject_count = 0
        self.approve_count = 0
        self.observe_count = 0

        # V2.0 异常保护状态
        self._anomaly_until: float = 0.0  # 异常暂停截止时间
        self._anomaly_reason: str = ""
        self._last_tick_price: dict[str, float] = {}
        self._last_tick_time: float = 0.0
        self._consecutive_errors: int = 0

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
        """每轮更新权益/回撤/日内亏损,必要时触发熔断"""
        eq = self.equity(last_prices)
        self.breaker.current_equity = eq

        dd, dd_breach = self.drawdown.update(eq)
        daily_breach = self.breaker.check_daily_loss(eq)

        if dd_breach:
            self.breaker.check_drawdown(dd)
            self._record_event("drawdown", detail=f"回撤{dd:.2%} 触发熔断", equity=eq)

        if daily_breach:
            self._record_event("breaker", detail="日内亏损超限", equity=eq)

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
            self._record_event(
                "anomaly", detail=f"价格异常 {symbol} {change:.2%}", equity=self.current_equity
            )
            return True
        return False

    def check_market_silence(self) -> bool:
        """行情静默检测: 超过阈值无 tick -> 暂停"""
        if self._last_tick_time <= 0:
            return False
        silent_for = time.time() - self._last_tick_time
        if silent_for > self.settings.risk_max_ws_silence_seconds:
            self._pause(f"行情静默{silent_for:.0f}秒")
            return True
        return False

    def record_execution_error(self) -> None:
        """执行失败计数,连续 3 次暂停"""
        self._consecutive_errors += 1
        if self._consecutive_errors >= 3:
            self._pause(f"连续执行失败{self._consecutive_errors}次")

    def record_execution_success(self) -> None:
        self._consecutive_errors = 0

    @property
    def anomaly_paused(self) -> bool:
        """异常暂停是否生效"""
        if self._anomaly_until <= 0:
            return False
        if time.time() >= self._anomaly_until:
            self.logger.info("异常保护解除", reason=self._anomaly_reason)
            self._anomaly_until = 0.0
            self._anomaly_reason = ""
            return False
        return True

    def _pause(self, reason: str) -> None:
        """触发交易暂停"""
        until = time.time() + self.settings.risk_anomaly_pause_seconds
        if self._anomaly_until < until:
            self._anomaly_until = until
            self._anomaly_reason = reason
            self.logger.error("交易暂停(异常保护)", reason=reason,
                              seconds=self.settings.risk_anomaly_pause_seconds)

    # ---------- 审批 ----------

    async def check(self, signal: Signal, last_prices: dict[str, float]) -> RiskDecision:
        """信号风控审批"""
        symbol = signal.symbol
        price = signal.price

        # 0. 观察档(策略标注未达执行阈值)
        if signal.reason and "观察档" in signal.reason_str:
            self.observe_count += 1
            return RiskDecision(approved=False, reason="观察档信号不执行", observed=True)

        # 1. 熔断中拒绝一切
        if self.breaker.is_open:
            return self._reject(signal, f"熔断中: {self.breaker.reason}")

        # 2. 异常保护暂停
        if self.anomaly_paused:
            return self._reject(signal, f"异常保护: {self._anomaly_reason}")

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
        """买入审批: 最大持仓限制(权益百分比)"""
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
            "anomaly_reason": self._anomaly_reason,
            "drawdown": self.drawdown.status(),
            "breaker": self.breaker.status(),
            "positions": {s: p.to_dict() for s, p in self.positions.positions.items()},
        }
