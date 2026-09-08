"""V11.4 P0-2 长跑仿真共享工具(deterministic, 不触真实交易所)。

提供:
- FakeClock: 可拨动逻辑时钟(替换 time.time, 保证幂等桶可分离 + 无真实等待);
- _signal / _sum / _count: 信号构造与 DB 聚合助手;
- assert_financial_invariants: 5 条财务守恒不变量(与 test_v125 同口径, 允许手续费);
- FakeRest / _paper_engine / _live_engine: 实盘故障注入桩与引擎装配;
- FILLED / TRADE / _half_trade 等合成成交字典。

5 条财务守恒不变量(每周期后断言):
  1. Base Asset  —— Σ BUY filled - Σ SELL filled == position.quantity(且无负仓);
  2. Lots        —— Σ open lot.quantity == position.quantity;
  3. Sell Alloc  —— Σ SellAllocation.quantity == Σ SELL filled;
  4. Cash        —— Σ USDT 变更 == Σ SELL quote - Σ BUY quote - Σ fee_quote;
  5. Equity      —— 权益 == 现金 + 标记持仓价值。
"""

from __future__ import annotations

from typing import Any, Optional

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
from at20_market.market_rest_client import BinanceAPIError
from at50_execution.execution_executor import ExecutionEngine
from at50_strategy.strategy_base import Signal, SignalSide
from at60_risk.risk_manager import RiskManager

SYMBOL = "SOLUSDT"


class FakeClock:
    """可拨动的逻辑时钟(替换 time.time)。"""

    def __init__(self, start: float = 1_700_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float = 1.0) -> None:
        self.now += seconds


def _signal(side: str, qty: float, price: float, strategy: str = "trend") -> Signal:
    return Signal(
        symbol=SYMBOL, strategy=strategy, side=SignalSide(side),
        price=price, quantity=qty,
    )


async def _sum(column, where=None) -> float:
    """Σ column(空集归 0); where 可选(SQLAlchemy 布尔表达式)。"""
    async with AsyncSessionLocal() as session:
        q = select(func.coalesce(func.sum(column), 0.0))
        if where is not None:
            q = q.where(where)
        return float((await session.execute(q)).scalar())


async def _count(model, where=None) -> int:
    """计数(where 可选)。"""
    async with AsyncSessionLocal() as session:
        q = select(func.count()).select_from(model)
        if where is not None:
            q = q.where(where)
        return int((await session.execute(q)).scalar())


async def assert_financial_invariants(
    engine: ExecutionEngine, rm: RiskManager, mark_price: float, *, check_paper: bool = True
) -> None:
    """断言 5 条财务守恒不变量(允许手续费, 与 test_v125 同口径)。

    - 不变量 1/2/3(持仓 / lot / 卖出分配)为「模式无关」, 纸面/实盘皆成立(基于 Order/Fill/Lot/Position);
    - 不变量 4(现金账本)与 5(权益)依赖 AccountLedger + paper.cash。AccountLedger 仅在
      `cash_before` 可得时落库(纸面 execute() 路径; 实盘 execute() 现金由交易所托管, cash_before=None,
      不落账本)。故实盘模式传 `check_paper=False` 跳过 4/5。

    mark_price: 用于第 5 条(权益)的标记价格, 取当前/最后成交价即可。
    """
    pos_qty = rm.positions.get(SYMBOL).quantity

    # 1. Base Asset: Σ BUY filled - Σ SELL filled == 持仓(且无负仓)
    buy = await _sum(Order.filled_quantity, and_(Order.symbol == SYMBOL, Order.side == "BUY"))
    sell = await _sum(Order.filled_quantity, and_(Order.symbol == SYMBOL, Order.side == "SELL"))
    assert buy - sell == pytest.approx(pos_qty, abs=1e-6), "base asset 守恒破坏"
    assert pos_qty >= -1e-9, "负持仓"
    db_qty = await _sum(Position.quantity, Position.symbol == SYMBOL)
    assert db_qty == pytest.approx(pos_qty, abs=1e-6), "持仓 DB 镜像不一致"

    # 2. Lots: Σ open lot.quantity == 持仓
    lot_sum = await _sum(
        PositionLot.quantity, and_(PositionLot.symbol == SYMBOL, PositionLot.status == "open")
    )
    assert lot_sum == pytest.approx(pos_qty, abs=1e-6), "lot 总和 != 持仓"

    # 3. Sell Allocation: Σ 卖出分配 == Σ SELL 成交
    alloc_sum = await _sum(SellAllocation.quantity, SellAllocation.symbol == SYMBOL)
    assert alloc_sum == pytest.approx(sell, abs=1e-6), "卖出分配 != SELL 成交"

    if not check_paper:
        return

    # 4. Cash(仅纸面; 实盘不落 AccountLedger): Σ USDT 变更 == Σ SELL quote - Σ BUY quote - Σ fee_quote
    usdt_change = await _sum(
        AccountLedger.change_amount,
        and_(AccountLedger.symbol == SYMBOL, AccountLedger.asset == "USDT"),
    )
    sell_quote = await _sum(
        OrderFill.quote_quantity, and_(OrderFill.symbol == SYMBOL, OrderFill.side == "SELL")
    )
    buy_quote = await _sum(
        OrderFill.quote_quantity, and_(OrderFill.symbol == SYMBOL, OrderFill.side == "BUY")
    )
    fee_quote = await _sum(OrderFill.fee_quote, OrderFill.symbol == SYMBOL)
    assert usdt_change == pytest.approx(sell_quote - buy_quote - fee_quote, abs=1e-6), "现金守恒破坏"
    assert engine.paper.cash == pytest.approx(
        engine.paper.initial_cash + usdt_change, abs=1e-6
    ), "纸面现金与账本不一致"

    # 5. Equity: 权益 == 现金 + 标记持仓价值
    #    (rm.equity 用 risk_initial_equity=100000, paper.initial_cash=100000, 二者一致;
    #     移动平均口径下该恒等式代数成立, 见 test_v125)
    assert rm.equity({SYMBOL: mark_price}) == pytest.approx(
        engine.paper.cash + pos_qty * mark_price, abs=1e-6
    ), "权益守恒破坏"


# ---------------------------------------------------------------------------
# 合成成交字典(实盘故障注入桩用)
# ---------------------------------------------------------------------------

def _filled(order_id: str = "100", qty: str = "1.0", quote: str = "100.0",
            price: str = "100.0") -> dict[str, str]:
    return {"orderId": order_id, "status": "FILLED",
            "executedQty": qty, "cummulativeQuoteQty": quote, "price": price}


def _trade(trade_id: int = 11, order_id: str = "100", price: str = "100.0",
           qty: str = "1.0", quote: str = "100.0", commission: str = "0",
           commission_asset: str = "USDT") -> dict[str, Any]:
    return {"id": trade_id, "orderId": order_id, "price": price, "qty": qty,
            "quoteQty": quote, "commission": commission,
            "commissionAsset": commission_asset, "time": 1700000000000}


class FakeRest:
    """可配置故障注入的 REST 桩(与 test_v107 同构; create_order 可抛超时/5xx)。"""

    def __init__(self, *, order_detail=None, order_queue=None, trades=None,
                 timeout=False, five_xx=False):
        self.order_detail = order_detail
        self.order_queue = list(order_queue or [])
        self.trades = trades or []
        self.timeout = timeout
        self.five_xx = five_xx
        self.created: list = []

    async def get_exchange_info(self, symbol=None):
        s = symbol or SYMBOL
        return {"symbols": [{"symbol": s, "filters": [
            {"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "100000", "stepSize": "0.001"},
            {"filterType": "PRICE_FILTER", "minPrice": "0.01", "maxPrice": "1000000", "tickSize": "0.01"},
            {"filterType": "MIN_NOTIONAL", "minNotional": "5.0"},
        ]}]}

    async def create_order(self, **kw):
        self.created.append(kw)
        if self.timeout:
            raise TimeoutError("network timeout")
        if self.five_xx:
            raise BinanceAPIError(500, -1, "server error")
        return {"orderId": "100"}

    async def get_order(self, symbol, order_id=None, orig_client_order_id=None):
        if self.order_queue:
            return self.order_queue.pop(0)
        return self.order_detail

    async def get_my_trades(self, symbol, limit=50, order_id=None):
        return self.trades

    async def cancel_order(self, symbol, order_id):
        return {}


def _paper_engine(rm: Optional[RiskManager] = None) -> ExecutionEngine:
    return ExecutionEngine(risk_manager=rm or RiskManager())


def _live_engine(rest: FakeRest, rm: Optional[RiskManager] = None) -> ExecutionEngine:
    e = ExecutionEngine(risk_manager=rm or RiskManager(), rest_client=rest)
    e.is_paper = False
    return e
