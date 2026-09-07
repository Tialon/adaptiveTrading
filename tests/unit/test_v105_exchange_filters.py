"""V10.5: 交易规则过滤器(ExchangeInfo)测试

从 /api/v3/exchangeInfo 解析 LOT_SIZE/PRICE_FILTER/MIN_NOTIONAL,
下单前对齐 stepSize/tickSize/minQty/minNotional(纯函数, 不查网络)。
"""

from decimal import Decimal

import pytest

from at50_execution.exchange_filters import SymbolFilters

EXCHANGE_INFO = {
    "symbols": [
        {
            "symbol": "SOLUSDT",
            "filters": [
                {"filterType": "PRICE_FILTER", "minPrice": "0.01000000",
                 "maxPrice": "10000.00000000", "tickSize": "0.01000000"},
                {"filterType": "LOT_SIZE", "minQty": "0.01000000",
                 "maxQty": "900000.00000000", "stepSize": "0.01000000"},
                {"filterType": "MIN_NOTIONAL", "minNotional": "10.00000000"},
            ],
        }
    ]
}


def _filters() -> SymbolFilters:
    return SymbolFilters.from_exchange_info("SOLUSDT", EXCHANGE_INFO)


class TestParse:
    def test_parse_fields(self):
        f = _filters()
        assert f.min_qty == Decimal("0.01")
        assert f.step_size == Decimal("0.01")
        assert f.tick_size == Decimal("0.01")
        assert f.min_notional == Decimal("10")
        assert f.max_qty == Decimal("900000")
        assert f.max_price == Decimal("10000")

    def test_missing_symbol_raises(self):
        with pytest.raises(ValueError):
            SymbolFilters.from_exchange_info("BTCUSDT", EXCHANGE_INFO)


class TestAdjust:
    def test_quantity_rounds_down_to_step(self):
        f = _filters()
        qty, price, violations = f.adjust(Decimal("1.005"), Decimal("100"))
        assert qty == Decimal("1.00")
        assert price == Decimal("100.00")
        assert violations == []

    def test_price_rounds_to_tick(self):
        f = _filters()
        _, price, _ = f.adjust(Decimal("1.00"), Decimal("100.006"))
        assert price == Decimal("100.01")

    def test_below_min_qty_violation(self):
        f = _filters()
        qty, _, violations = f.adjust(Decimal("0.005"), Decimal("100"))
        assert qty == Decimal("0.00")
        assert any("minQty" in v for v in violations)

    def test_below_min_notional_violation(self):
        f = _filters()
        _, _, violations = f.adjust(Decimal("1.00"), Decimal("5"))
        assert any("minNotional" in v for v in violations)

    def test_boundary_min_notional_ok(self):
        f = _filters()
        # 0.01 qty * 1000 price = 10.0 == minNotional, 不触发
        _, _, violations = f.adjust(Decimal("0.01"), Decimal("1000"))
        assert violations == []


class TestWiring:
    async def test_ensure_filters_loads(self, db_tables):
        from at50_execution.execution_executor import ExecutionEngine
        from at60_risk.risk_manager import RiskManager

        class FakeRest:
            async def get_exchange_info(self, symbol):
                return EXCHANGE_INFO

        eng = ExecutionEngine(risk_manager=RiskManager(), rest_client=FakeRest())
        f = await eng._ensure_filters("SOLUSDT")
        assert f is not None
        assert f.step_size == Decimal("0.01")
        # 命中缓存
        assert await eng._ensure_filters("SOLUSDT") is not None
