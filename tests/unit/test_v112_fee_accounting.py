"""V11.1(P0-2): 统一手续费计价(FeeCalculator)+ 不可计价手续费降级测试

验证:
- FeeCalculator: USDT/SOL 可折算; 其它(BNB) -> unpriced(不静默 fee=0);
- _compute_fill_metrics 返回 fee_unpriced 标记;
- _record_fills 逐笔落 fee_quote / fee_valuation_status;
- 不可计价手续费在摄入路径触发降级(pause), 可计价则不触发。
"""

import pytest

from at01_common.database import AsyncSessionLocal
from at01_common.models import OrderFill
from at50_execution.execution_executor import ExecutionEngine, _compute_fill_metrics
from at50_execution.fee_calculator import FeeCalculator, UNPRICED, PRICED, ZERO
from at60_risk.risk_manager import RiskManager

SYMBOL = "SOLUSDT"


class _FakeRest:
    def __init__(self, fills):
        self.fills = fills

    async def get_my_trades(self, symbol, limit=None, order_id=None):
        return self.fills


class TestFeeCalculator:
    def test_zero_commission(self):
        ff = FeeCalculator(SYMBOL).fill_fee({"commission": "0", "commissionAsset": "USDT", "price": "100"})
        assert ff.fee_quote == 0.0
        assert ff.valuation_status == ZERO

    def test_quote_asset_priced(self):
        ff = FeeCalculator(SYMBOL).fill_fee({"commission": "0.5", "commissionAsset": "USDT", "price": "100"})
        assert ff.fee_quote == pytest.approx(0.5)
        assert ff.valuation_status == PRICED

    def test_base_asset_priced(self):
        # SOL 手续费 -> 按成交价折算 quote
        ff = FeeCalculator(SYMBOL).fill_fee({"commission": "0.01", "commissionAsset": "SOL", "price": "100.5"})
        assert ff.fee_quote == pytest.approx(0.01 * 100.5)
        assert ff.valuation_status == PRICED

    def test_unpriced_other_asset(self):
        # BNB 手续费(币安默认抵扣) -> unpriced, 不静默记为 0
        ff = FeeCalculator(SYMBOL).fill_fee({"commission": "0.003", "commissionAsset": "BNB", "price": "100"})
        assert ff.fee_quote == 0.0
        assert ff.valuation_status == UNPRICED

    def test_total_aggregates_unpriced_flag(self):
        fills = [
            {"commission": "0.5", "commissionAsset": "USDT", "price": "100"},
            {"commission": "0.003", "commissionAsset": "BNB", "price": "100"},
        ]
        result = FeeCalculator(SYMBOL).total(fills)
        assert result.fee_quote == pytest.approx(0.5)  # 只计入可折算部分
        assert result.unpriced is True
        assert result.unpriced_assets == ["BNB"]


class TestFillMetricsUnpriced:
    def test_compute_fill_metrics_unpriced_flag(self):
        fills = [
            {"qty": "1.0", "quoteQty": "100.0", "price": "100.0",
             "commission": "0.003", "commissionAsset": "BNB"},
        ]
        avg, fee, unpriced = _compute_fill_metrics(SYMBOL, fills)
        assert avg == pytest.approx(100.0)
        assert fee == 0.0
        assert unpriced is True

    def test_compute_fill_metrics_no_unpriced(self):
        fills = [
            {"qty": "1.0", "quoteQty": "100.0", "price": "100.0",
             "commission": "0.1", "commissionAsset": "USDT"},
        ]
        _, fee, unpriced = _compute_fill_metrics(SYMBOL, fills)
        assert fee == pytest.approx(0.1)
        assert unpriced is False


class TestRecordFillsFeeStatus:
    async def test_record_fills_persists_fee_status(self, db_tables):
        engine = ExecutionEngine(risk_manager=RiskManager())
        fills = [
            {"id": 1, "orderId": "100", "price": "100.0", "qty": "0.5",
             "quoteQty": "50.0", "commission": "0.05", "commissionAsset": "USDT", "time": 1},
            {"id": 2, "orderId": "100", "price": "100.0", "qty": "0.5",
             "quoteQty": "50.0", "commission": "0.003", "commissionAsset": "BNB", "time": 2},
        ]
        await engine._record_fills(
            order_id=None, client_order_id="cid-1", exchange_order_id="100",
            symbol=SYMBOL, side="BUY", fills=fills,
        )
        from sqlalchemy import select

        async with AsyncSessionLocal() as session:
            rows = (await session.execute(select(OrderFill).order_by(OrderFill.exchange_trade_id))).scalars().all()
            assert len(rows) == 2
            by_tid = {r.exchange_trade_id: r for r in rows}
            assert by_tid[1].fee_valuation_status == PRICED
            assert by_tid[1].fee_quote == pytest.approx(0.05)
            assert by_tid[2].fee_valuation_status == UNPRICED
            assert by_tid[2].fee_quote == 0.0  # 显式 0 + unpriced 标记(不静默)


class TestIngestFillsDegrade:
    async def test_ingest_fills_pauses_on_unpriced(self, db_tables):
        rm = RiskManager()
        engine = ExecutionEngine(risk_manager=rm, rest_client=_FakeRest([
            {"id": 1, "orderId": "100", "price": "100.0", "qty": "1.0",
             "quoteQty": "100.0", "commission": "0.003", "commissionAsset": "BNB", "time": 1},
        ]))
        result = await engine._ingest_fills(
            order_id=None, client_order_id="cid-1", exchange_order_id="100",
            symbol=SYMBOL, side="BUY",
        )
        assert result is not None
        avg, fee, unpriced = result
        assert unpriced is True
        assert rm.anomaly_paused is True  # 不可计价 -> 降级(pause), 不静默

    async def test_ingest_fills_no_pause_on_priced(self, db_tables):
        rm = RiskManager()
        engine = ExecutionEngine(risk_manager=rm, rest_client=_FakeRest([
            {"id": 1, "orderId": "100", "price": "100.0", "qty": "1.0",
             "quoteQty": "100.0", "commission": "0.1", "commissionAsset": "USDT", "time": 1},
        ]))
        result = await engine._ingest_fills(
            order_id=None, client_order_id="cid-1", exchange_order_id="100",
            symbol=SYMBOL, side="BUY",
        )
        assert result is not None
        _, fee, unpriced = result
        assert fee == pytest.approx(0.1)
        assert unpriced is False
        assert rm.anomaly_paused is False
