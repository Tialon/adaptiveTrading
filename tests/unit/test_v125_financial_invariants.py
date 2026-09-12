"""V11.2 P0-6 财务不变量最终审计(统一 invariant test)。

对贯穿「订单→成交→账本→持仓→lot」链路的 5 条财务守恒不变量做最终收口审计,
全部计算**允许手续费**(fee 显式参与恒等式, 不假设零手续费):

1. Base Asset   —— Σ BUY filled - Σ SELL filled == position.quantity(订单 ↔ 持仓);
2. Lots         —— Σ open lot.quantity == position.quantity(lot ↔ 持仓);
3. Sell Alloc   —— Σ SellAllocation.quantity == 该笔 SELL filled(卖出分配 ↔ 订单);
4. Cash         —— Σ AccountLedger.USDT 变更 == Σ SELL quote - Σ BUY quote - Σ fee_quote(账本 ↔ 成交);
5. Equity       —— 平仓后 cash == initial_cash + realized_pnl(现金 ↔ 持仓已实现盈亏);
6. 守恒破坏     —— 任一守恒失败(此处用 lot 总和 ≠ 持仓)经对账矩阵收敛后**不能继续正常开仓**。
"""

import pytest
from sqlalchemy import and_, func, select

from at01_common.database import AsyncSessionLocal
from at01_common.models import (
    AccountLedger,
    Order,
    OrderFill,
    Position,
    PositionLot,
    SellAllocation,
)
from at60_execution.execution_executor import ExecutionEngine
from at30_strategy.strategy_base import Signal, SignalSide
from at50_risk.risk_manager import RiskManager

SYMBOL = "SOLUSDT"


def _signal(side: str, qty: float, price: float) -> Signal:
    return Signal(symbol=SYMBOL, strategy="trend", side=SignalSide(side),
                  price=price, quantity=qty)


async def _sum(column, where=None) -> float:
    """Σ column(空集归 0); where 可选(SQLAlchemy 布尔表达式)。"""
    async with AsyncSessionLocal() as session:
        q = select(func.coalesce(func.sum(column), 0.0))
        if where is not None:
            q = q.where(where)
        return (await session.execute(q)).scalar()


class TestFinancialInvariants:
    # 1. Base Asset 守恒(订单 ↔ 持仓, 无负仓)
    async def test_base_asset_conservation(self, db_tables):
        rm = RiskManager()
        engine = ExecutionEngine(risk_manager=rm)
        await engine.execute(_signal("BUY", 1.5, 100.0))
        await engine.execute(_signal("SELL", 0.5, 105.0))

        buy = await _sum(Order.filled_quantity, and_(Order.symbol == SYMBOL, Order.side == "BUY"))
        sell = await _sum(Order.filled_quantity, and_(Order.symbol == SYMBOL, Order.side == "SELL"))
        pos_qty = rm.positions.get(SYMBOL).quantity

        assert buy - sell == pytest.approx(pos_qty)
        assert pos_qty == pytest.approx(1.0)
        assert pos_qty >= 0.0  # 无负仓

        # 持仓 DB 镜像一致
        db_qty = await _sum(Position.quantity, Position.symbol == SYMBOL)
        assert db_qty == pytest.approx(pos_qty)

    # 2. Lots 守恒(lot ↔ 持仓)
    async def test_lot_sum_equals_position(self, db_tables):
        rm = RiskManager()
        engine = ExecutionEngine(risk_manager=rm)
        await engine.execute(_signal("BUY", 2.0, 100.0))
        await engine.execute(_signal("BUY", 1.0, 120.0))
        await engine.execute(_signal("SELL", 0.7, 130.0))

        pos_qty = rm.positions.get(SYMBOL).quantity  # 2.3
        lot_sum = await _sum(PositionLot.quantity, and_(
            PositionLot.symbol == SYMBOL, PositionLot.status == "open"))
        assert lot_sum == pytest.approx(pos_qty)
        # 对账不变量不报差异
        assert engine.lot_tracker.reconcile(SYMBOL, pos_qty) is None

    # 3. Sell Allocation 守恒(卖出分配 ↔ 该笔 SELL 订单)
    async def test_sell_allocation_equals_sell_quantity(self, db_tables):
        rm = RiskManager()
        engine = ExecutionEngine(risk_manager=rm)
        await engine.execute(_signal("BUY", 2.0, 100.0))
        result = await engine.execute(_signal("SELL", 0.7, 130.0))
        sell_cid = result["client_order_id"]

        alloc_sum = await _sum(
            SellAllocation.quantity, SellAllocation.sell_client_order_id == sell_cid)
        sell_filled = await _sum(Order.filled_quantity, Order.client_order_id == sell_cid)

        assert alloc_sum == pytest.approx(sell_filled)
        assert alloc_sum == pytest.approx(0.7)
        # 卖出分配不超卖出量(不跨 lot 超卖)
        assert alloc_sum <= sell_filled

    # 4. Cash 守恒(账本 ↔ 成交, 允许手续费)
    async def test_cash_conservation_with_fee(self, db_tables):
        rm = RiskManager()
        engine = ExecutionEngine(risk_manager=rm)
        await engine.execute(_signal("BUY", 1.0, 100.0))
        await engine.execute(_signal("SELL", 1.0, 110.0))

        usdt_change = await _sum(
            AccountLedger.change_amount,
            and_(AccountLedger.symbol == SYMBOL, AccountLedger.asset == "USDT"),
        )
        sell_quote = await _sum(
            OrderFill.quote_quantity, and_(OrderFill.symbol == SYMBOL, OrderFill.side == "SELL"))
        buy_quote = await _sum(
            OrderFill.quote_quantity, and_(OrderFill.symbol == SYMBOL, OrderFill.side == "BUY"))
        fee_quote = await _sum(OrderFill.fee_quote, OrderFill.symbol == SYMBOL)

        # Σ USDT 变更 == Σ SELL 成交额 - Σ BUY 成交额 - Σ 手续费
        assert usdt_change == pytest.approx(sell_quote - buy_quote - fee_quote)
        # 纸面现金与账本净变动自洽
        assert engine.paper.cash == pytest.approx(engine.paper.initial_cash + usdt_change)

    # 5. Equity 守恒(平仓后现金 ↔ 已实现盈亏, 允许手续费)
    async def test_equity_round_trip_conservation(self, db_tables):
        rm = RiskManager()
        engine = ExecutionEngine(risk_manager=rm)
        await engine.execute(_signal("BUY", 1.0, 100.0))
        await engine.execute(_signal("SELL", 1.0, 110.0))

        pos = rm.positions.get(SYMBOL)
        assert pos.quantity == pytest.approx(0.0)

        # 平仓后: 现金变动 == 累计已实现盈亏(FIFO, 已扣买卖手续费)
        assert engine.paper.cash == pytest.approx(
            engine.paper.initial_cash + pos.realized_pnl)
        # 权益 = 初始 + 已实现(未实现为 0)
        assert rm.equity({SYMBOL: 110.0}) == pytest.approx(
            engine.paper.initial_cash + pos.realized_pnl)

    # 6. 守恒破坏 → 对账矩阵 → 不能继续正常开仓
    async def test_conservation_failure_blocks_open(self, db_tables):
        from at60_execution.reconciliation_matrix import ReconciliationMatrix, Severity
        from at50_risk.fund_circuit_breaker import FundCircuitBreaker
        from at50_risk.system_lifecycle import SystemLifecycle
        from at50_risk.trading_gate import TradingGate

        rm = RiskManager()
        engine = ExecutionEngine(risk_manager=rm)
        await engine.execute(_signal("BUY", 2.0, 100.0))

        # 篡改内存 lot, 破坏「lot 总和 == 持仓」守恒
        engine.lot_tracker.lots[SYMBOL][0]["quantity"] = 1.0
        diff = engine.lot_tracker.reconcile(SYMBOL, rm.positions.get(SYMBOL).quantity)
        assert diff is not None  # 守恒破坏被检出

        # 守恒差异 → 对账矩阵(单一内部对账器 → RECOVERY_REQUIRED, 不冒进 kill)
        matrix = ReconciliationMatrix()
        matrix.ingest("lot", [{"type": "lot_sum_mismatch", "symbol": SYMBOL, **diff}])
        verdict = matrix.verdict()
        assert verdict.severity is Severity.RECOVERY_REQUIRED

        # 复用 run.py 语义: RECOVERY_REQUIRED → 暂停 → 统一闸门禁开仓
        rm.pause(verdict.reasons[0])
        lifecycle = SystemLifecycle()
        lifecycle.warm_up()
        lifecycle.sync()
        lifecycle.self_check()
        lifecycle.ready()
        lifecycle.start_trading()
        gate = TradingGate(rm, lifecycle, FundCircuitBreaker())

        ok, reason = gate.can_open_position()
        assert not ok, f"守恒失败后仍可开仓, 违反不变量(原因: {reason})"
