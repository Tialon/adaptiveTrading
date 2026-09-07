"""V10.1: Order→Fill→Ledger 链集成测试

- 实盘链(is_paper=False): create_order + get_order + myTrades ->
  Order 落 FILLED + OrderFill 逐笔落库(真实手续费)。
- 纸面链: execute() 合成 OrderFill + AccountLedger 落 commission。
"""

import pytest
from sqlalchemy import select

from at01_common.database import AsyncSessionLocal
from at01_common.models import AccountLedger, Order, OrderFill
from at50_execution.execution_executor import ExecutionEngine
from at50_strategy.strategy_base import Signal, SignalSide
from at60_risk.risk_manager import RiskManager


class FakeRest:
    def __init__(self, order_detail=None, trades=None):
        self._order_detail = order_detail or {}
        self._trades = trades or []
        self.created: list = []

    async def create_order(self, **kw):
        self.created.append(kw)
        return {"orderId": "100"}

    async def get_order(self, symbol, order_id=None, orig_client_order_id=None):
        return self._order_detail

    async def get_my_trades(self, symbol, limit=50, order_id=None):
        return self._trades

    async def cancel_order(self, symbol, order_id):
        return {}


class TestLiveChain:
    async def test_order_to_fill_real_fee(self, db_tables):
        trades = [{
            "id": 11, "orderId": "100", "price": "100.0", "qty": "1.0",
            "quoteQty": "100.0", "commission": "0.10", "commissionAsset": "USDT",
            "time": 1700000000000,
        }]
        rest = FakeRest(
            order_detail={"status": "FILLED", "executedQty": "1.0",
                          "cummulativeQuoteQty": "100.0"},
            trades=trades,
        )
        rm = RiskManager()
        engine = ExecutionEngine(risk_manager=rm, rest_client=rest)
        engine.is_paper = False  # 强制实盘路径(conftest 默认 paper)

        sig = Signal(symbol="SOLUSDT", strategy="entry", side=SignalSide.BUY,
                     price=100.0, quantity=1.0)
        result = await engine.execute(sig)
        assert result is not None
        assert result["status"] == "FILLED"

        async with AsyncSessionLocal() as session:
            order = (await session.execute(
                select(Order).where(Order.client_order_id == result["client_order_id"])
            )).scalar_one()
            assert order.status == "FILLED"
            assert order.exchange_order_id == "100"
            fills = (await session.execute(
                select(OrderFill).where(OrderFill.exchange_order_id == "100")
            )).scalars().all()
            assert len(fills) == 1
            assert fills[0].commission == pytest.approx(0.10)
            assert fills[0].commission_asset == "USDT"


class TestPaperChain:
    async def test_order_fill_ledger_commission(self, db_tables):
        rm = RiskManager()
        engine = ExecutionEngine(risk_manager=rm)  # is_paper=True (conftest)

        sig = Signal(symbol="SOLUSDT", strategy="entry", side=SignalSide.BUY,
                     price=100.0, quantity=1.0)
        result = await engine.execute(sig)
        assert result is not None
        assert result["status"] == "FILLED"

        cid = result["client_order_id"]
        async with AsyncSessionLocal() as session:
            order = (await session.execute(
                select(Order).where(Order.client_order_id == cid)
            )).scalar_one()
            assert order.status == "FILLED"
            fills = (await session.execute(
                select(OrderFill).where(OrderFill.client_order_id == cid)
            )).scalars().all()
            assert len(fills) == 1
            assert fills[0].commission > 0  # 纸面合成 fill 手续费 = fee_paid
            ledgers = (await session.execute(
                select(AccountLedger).where(AccountLedger.related_order_id == cid)
            )).scalars().all()
            assert len(ledgers) == 2  # USDT + SOL 两行
            assert all(r.commission > 0 for r in ledgers)
            assert all(r.commission_asset == "USDT" for r in ledgers)
