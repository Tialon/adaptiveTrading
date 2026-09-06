"""
风控管理器

审批链:信号 -> 仓位限额 -> 熔断/回撤/日内亏损 -> 风控定价(数量)
输出:RiskDecision(approved / rejected)
"""

import time
from dataclasses import dataclass
from typing import Any, Optional

from common.config.settings import get_settings
from common.utils.logger import LoggerMixin
from risk.breaker import CircuitBreaker
from risk.drawdown import DrawdownController
from risk.position import PositionManager
from strategy.base import Signal, SignalSide


@dataclass
class RiskDecision:
    """风控决策"""

    approved: bool
    reason: str = ""
    quantity: float = 0.0  # 批准的数量
    price: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "reason": self.reason,
            "quantity": self.quantity,
            "price": self.price,
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

    # ---------- 权益 ----------

    def equity(self, last_prices: dict[str, float]) -> float:
        """总权益 = 现金近似(初始权益+已实现盈亏) + 未实现盈亏"""
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

    # ---------- 审批 ----------

    async def check(self, signal: Signal, last_prices: dict[str, float]) -> RiskDecision:
        """信号风控审批"""
        symbol = signal.symbol
        price = signal.price

        # 1. 熔断中拒绝一切
        if self.breaker.is_open:
            return self._reject(signal, f"熔断中: {self.breaker.reason}")

        # 2. 价格有效性
        if price <= 0:
            return self._reject(signal, "价格无效")

        # 3. 目标数量
        if signal.quantity:
            quantity = signal.quantity
        elif signal.quote_amount:
            quantity = signal.quote_amount / price
        else:
            return self._reject(signal, "信号缺少数量/金额")

        # 4. 单笔金额限制
        order_quote = quantity * price
        if order_quote > self.settings.risk_max_single_order_quote:
            quantity = self.settings.risk_max_single_order_quote / price
            order_quote = self.settings.risk_max_single_order_quote
            self.logger.warning("单笔金额超限,已缩量", symbol=symbol, capped=order_quote)

        if order_quote < 10:  # 币安最小名义价值
            return self._reject(signal, f"订单金额过小 {order_quote:.2f} USDT")

        if signal.side == SignalSide.BUY:
            return await self._check_buy(signal, quantity, price, last_prices)
        return await self._check_sell(signal, quantity, price, last_prices)

    async def _check_buy(
        self, signal: Signal, quantity: float, price: float, last_prices: dict[str, float]
    ) -> RiskDecision:
        """买入审批:最大持仓限制"""
        symbol = signal.symbol
        pos = self.positions.get(symbol)
        current_quote = pos.quantity * (last_prices.get(symbol, price))
        new_quote = current_quote + quantity * price

        if new_quote > self.settings.risk_max_position_quote:
            room = self.settings.risk_max_position_quote - current_quote
            if room < 10:
                return self._reject(
                    signal,
                    f"持仓超限: 当前{current_quote:.0f} + 新增{quantity*price:.0f} > "
                    f"{self.settings.risk_max_position_quote:.0f}",
                )
            # 缩量至剩余额度
            quantity = room / price
            self.logger.warning("买入缩量至持仓限额内", symbol=symbol, new_qty=quantity)

        return self._approve(signal, quantity, price)

    async def _check_sell(
        self, signal: Signal, quantity: float, price: float, last_prices: dict[str, float]
    ) -> RiskDecision:
        """卖出审批:不能卖出超过持仓"""
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
            from common.config.database import AsyncSessionLocal
            from common.models import RiskEvent

            async with AsyncSessionLocal() as session:
                session.add(RiskEvent(event_type=event_type, detail=detail, equity=equity))
                await session.commit()
        except Exception:
            self.logger.exception("风控事件持久化失败")

    def status(self) -> dict[str, Any]:
        return {
            "approve_count": self.approve_count,
            "reject_count": self.reject_count,
            "drawdown": self.drawdown.status(),
            "breaker": self.breaker.status(),
            "positions": {s: p.to_dict() for s, p in self.positions.positions.items()},
        }
