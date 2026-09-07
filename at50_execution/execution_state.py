"""
订单与交易状态机(V3.0)

Order State Machine(单笔订单):
    CREATE -> SUBMIT -> OPEN -> PARTIAL_FILL -> FILLED
                   或 -> CANCELED / REJECTED / EXPIRED

Trade State Machine(每标的交易周期, 防重复建仓):
    IDLE -> ENTRY_PENDING -> HOLDING -> EXIT_PENDING -> CLOSED -> IDLE
              (未成交回IDLE)             (部分卖出回HOLDING)
"""

from enum import Enum

from at01_common.logger import LoggerMixin


class OrderState(str, Enum):
    """订单状态机"""

    CREATE = "CREATE"          # 已创建(本地)
    SUBMIT = "SUBMIT"          # 已提交交易所/模拟器
    OPEN = "OPEN"              # 交易所已接受, 未成交
    PARTIAL_FILL = "PARTIAL_FILL"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"

    @property
    def terminal(self) -> bool:
        """终态"""
        return self in (self.FILLED, self.CANCELED, self.REJECTED, self.EXPIRED)

    def can_transition(self, target: "OrderState") -> bool:
        if self.terminal:
            return False
        return target.value in ORDER_TRANSITIONS.get(self.value, set())


class TradeState(str, Enum):
    """交易周期状态机(每标的)"""

    IDLE = "IDLE"                # 空仓等待
    ENTRY_PENDING = "ENTRY_PENDING"  # 买单已提交未成交
    HOLDING = "HOLDING"          # 持仓中
    EXIT_PENDING = "EXIT_PENDING"    # 卖单已提交(可能部分)
    CLOSED = "CLOSED"            # 本轮平仓完成

    def can_transition(self, target: "TradeState") -> bool:
        return target.value in TRADE_TRANSITIONS.get(self.value, set())


class TradeStateMachine(LoggerMixin):
    """各标的交易状态机管理(防止状态外的重复买入等)"""

    def __init__(self):
        self._states: dict[str, TradeState] = {}

    def get(self, symbol: str) -> TradeState:
        return self._states.get(symbol, TradeState.IDLE)

    def set(self, symbol: str, target: TradeState) -> bool:
        """带校验的状态迁移, 非法迁移拒绝并告警"""
        current = self.get(symbol)
        if current is target:
            return True
        if not current.can_transition(target):
            self.logger.warning(
                "非法交易状态迁移", symbol=symbol, current=current.value, target=target.value
            )
            return False
        self._states[symbol] = target
        self.logger.info("交易状态", symbol=symbol, state=target.value)
        return True

    def on_order_submitted(self, symbol: str, side: str) -> None:
        """订单提交: BUY -> ENTRY_PENDING, SELL -> EXIT_PENDING"""
        if side.upper() == "BUY":
            self.set(symbol, TradeState.ENTRY_PENDING)
        else:
            self.set(symbol, TradeState.EXIT_PENDING)

    def on_order_filled(self, symbol: str, side: str, remaining_qty: float) -> None:
        """订单成交: 按剩余持仓推进状态"""
        current = self.get(symbol)
        if side.upper() == "BUY":
            # 纸面模式可能未经过 ENTRY_PENDING(无挂单阶段), 先补齐
            if current is TradeState.IDLE:
                self.set(symbol, TradeState.ENTRY_PENDING)
            self.set(symbol, TradeState.HOLDING)
        else:
            if current is TradeState.IDLE:
                return  # 历史持仓的卖出, 不影响状态周期
            # 纸面模式可能未经过 EXIT_PENDING(无挂单阶段), 先补齐
            if current is TradeState.HOLDING:
                self.set(symbol, TradeState.EXIT_PENDING)
            if remaining_qty <= 0:
                self.set(symbol, TradeState.CLOSED)
                self.set(symbol, TradeState.IDLE)
            else:
                self.set(symbol, TradeState.HOLDING)

    def on_order_canceled(self, symbol: str, side: str, position_qty: float) -> None:
        """订单取消/拒绝"""
        if side.upper() == "BUY" and position_qty <= 0:
            self.set(symbol, TradeState.IDLE)
        elif side.upper() == "SELL":
            self.set(symbol, TradeState.HOLDING)

    def can_buy(self, symbol: str) -> bool:
        """买入闸门: IDLE 或 CLOSED(允许新一轮) 时可买"""
        return self.get(symbol) in (TradeState.IDLE,)

    def status(self) -> dict[str, str]:
        return {s: st.value for s, st in self._states.items()}

    # ---------- 持久化(V8) ----------

    async def load_from_db(self) -> None:
        """启动时加载各标的交易状态"""
        from sqlalchemy import select

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import TradeStateRow

        try:
            async with AsyncSessionLocal() as session:
                rows = (await session.execute(select(TradeStateRow))).scalars().all()
                for r in rows:
                    try:
                        self._states[r.symbol] = TradeState(r.state)
                    except ValueError:
                        self.logger.warning("非法交易状态忽略", symbol=r.symbol, state=r.state)
            self.logger.info("交易状态已加载", count=len(self._states))
        except Exception:
            self.logger.exception("交易状态加载失败")

    async def persist(self, symbol: str) -> None:
        """持久化单标的交易状态(upsert)"""
        from sqlalchemy import select

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import TradeStateRow

        state = self._states.get(symbol)
        if state is None:
            return
        try:
            async with AsyncSessionLocal() as session:
                row = (
                    await session.execute(
                        select(TradeStateRow).where(TradeStateRow.symbol == symbol)
                    )
                ).scalar_one_or_none()
                if row is None:
                    row = TradeStateRow(symbol=symbol)
                    session.add(row)
                row.state = state.value
                await session.commit()
        except Exception:
            self.logger.exception("交易状态持久化失败", symbol=symbol)

    def reconcile_with_positions(self, held_symbols: "set[str]") -> None:
        """对账: 有持仓但状态为 IDLE/CLOSED 的标的 -> 置 HOLDING(重启后状态与持仓一致)"""
        for symbol in held_symbols:
            st = self._states.get(symbol)
            if st in (None, TradeState.IDLE, TradeState.CLOSED):
                self._states[symbol] = TradeState.HOLDING
                self.logger.info("交易状态对账置 HOLDING", symbol=symbol)


# 模块级迁移表(enum 类体内 dict 属性会被成员化, 必须外置)
ORDER_TRANSITIONS = {
    "CREATE": {"SUBMIT", "CANCELED", "REJECTED"},
    "SUBMIT": {"OPEN", "FILLED", "REJECTED", "EXPIRED", "PARTIAL_FILL"},
    "OPEN": {"PARTIAL_FILL", "FILLED", "CANCELED", "EXPIRED"},
    "PARTIAL_FILL": {"PARTIAL_FILL", "FILLED", "CANCELED", "EXPIRED"},
}

TRADE_TRANSITIONS = {
    "IDLE": {"ENTRY_PENDING", "HOLDING"},  # HOLDING: 纸面模式无挂单阶段, 成交即持仓
    "ENTRY_PENDING": {"IDLE", "HOLDING"},      # 未成交撤单回 IDLE / 成交进 HOLDING
    "HOLDING": {"EXIT_PENDING", "HOLDING"},    # 部分卖出仍 HOLDING
    "EXIT_PENDING": {"HOLDING", "CLOSED"},     # 部分成交回 HOLDING / 全平 CLOSED
    "CLOSED": {"IDLE", "ENTRY_PENDING"},       # 开新一轮
}
