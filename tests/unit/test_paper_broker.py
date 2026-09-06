"""执行引擎单元测试:纸面交易"""

import pytest

from at50_execution.execution_paper_broker import PaperBroker
from at60_risk.risk_manager import RiskManager
from at50_strategy.strategy_base import Signal, SignalSide


class TestPaperBroker:
    async def test_market_buy_fills(self):
        broker = PaperBroker(initial_cash=10000.0, fee_rate=0.001)
        order = await broker.create_order(
            symbol="BTCUSDT", side="BUY", order_type="MARKET",
            quantity=1.0, price=None, last_price=100.0,
        )
        assert order.status == "FILLED"
        assert order.filled_quantity == 1.0
        assert order.avg_fill_price > 100.0  # 含滑点
        # 现金减少
        assert broker.cash < 10000.0

    async def test_market_sell_fills(self):
        broker = PaperBroker(initial_cash=10000.0, fee_rate=0.001)
        order = await broker.create_order(
            symbol="BTCUSDT", side="SELL", order_type="MARKET",
            quantity=1.0, price=None, last_price=100.0,
        )
        assert order.status == "FILLED"
        assert broker.cash > 10000.0

    async def test_insufficient_cash_rejected(self):
        broker = PaperBroker(initial_cash=50.0, fee_rate=0.001)
        order = await broker.create_order(
            symbol="BTCUSDT", side="BUY", order_type="MARKET",
            quantity=1.0, price=None, last_price=100.0,
        )
        assert order.status == "REJECTED"

    async def test_fee_accounted(self):
        broker = PaperBroker(initial_cash=10000.0, fee_rate=0.01)
        await broker.create_order(
            symbol="BTCUSDT", side="BUY", order_type="MARKET",
            quantity=1.0, price=None, last_price=100.0,
        )
        # 现金 = 10000 - (100*(1+slip)) - fee
        expected_quote = 100.0 * (1 + 0.0002)
        expected = 10000.0 - expected_quote - expected_quote * 0.01
        assert broker.cash == pytest.approx(expected)


class TestExecutionPipeline:
    async def test_paper_execution_updates_position(self):
        """信号 -> 执行 -> 持仓全链路(纸面)"""
        from at50_execution.execution_executor import ExecutionEngine

        rm = RiskManager()
        rm.positions.positions.clear()
        engine = ExecutionEngine(risk_manager=rm)

        sig = Signal(
            symbol="BTCUSDT", strategy="test", side=SignalSide.BUY,
            price=100.0, quantity=1.0,
        )
        result = await engine.execute(sig)
        assert result is not None
        assert result["status"] == "FILLED"
        assert result["fill_qty"] == 1.0

        pos = rm.positions.get("BTCUSDT")
        assert pos.quantity == 1.0
        assert pos.avg_price > 100.0

    async def test_sell_after_buy(self):
        from at50_execution.execution_executor import ExecutionEngine

        rm = RiskManager()
        rm.positions.positions.clear()
        engine = ExecutionEngine(risk_manager=rm)

        buy = Signal(
            symbol="BTCUSDT", strategy="test", side=SignalSide.BUY,
            price=100.0, quantity=1.0,
        )
        await engine.execute(buy)

        sell = Signal(
            symbol="BTCUSDT", strategy="test", side=SignalSide.SELL,
            price=110.0, quantity=1.0,
        )
        result = await engine.execute(sell)
        assert result["status"] == "FILLED"

        pos = rm.positions.get("BTCUSDT")
        assert pos.quantity == 0.0
        assert pos.realized_pnl > 0
