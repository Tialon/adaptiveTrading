"""V10.6(P1-f): ExchangeInfo 失败 -> 禁 BUY(safety 优先)测试

验证:
- 交易规则拉取失败(exchangeInfo 异常)时, 实盘 BUY 被本地拒绝、不下单;
- SELL 减仓仍放行(不新增敞口, 失败无损失);
- 交易规则正常加载时 BUY 不受影响。
"""


from at60_execution.execution_executor import ExecutionEngine
from at30_strategy.strategy_base import Signal, SignalSide
from at50_risk.risk_manager import RiskManager


def _make_signal(**kw) -> Signal:
    base = dict(symbol="SOLUSDT", strategy="grid", side=SignalSide.BUY,
                price=100.0, quantity=1.0)
    base.update(kw)
    return Signal(**base)


class _MockRest:
    """REST 假实现: exchangeInfo 可配置为失败"""

    def __init__(self, exchange_info_error=None):
        self.exchange_info_error = exchange_info_error
        self.create_calls = 0
        self.create_result = {"orderId": "100"}
        self.order_detail = {"status": "FILLED", "executedQty": "1.0",
                             "cummulativeQuoteQty": "100.0"}
        self.trades: list = []

    async def get_exchange_info(self, symbol=None):
        if self.exchange_info_error is not None:
            raise self.exchange_info_error
        s = symbol or "SOLUSDT"
        return {"symbols": [{"symbol": s, "filters": [
            {"filterType": "LOT_SIZE", "minQty": "0.001", "stepSize": "0.001"},
            {"filterType": "PRICE_FILTER", "minPrice": "0.01", "tickSize": "0.01"},
            {"filterType": "MIN_NOTIONAL", "minNotional": "5.0"},
        ]}]}

    async def create_order(self, **kw):
        self.create_calls += 1
        return self.create_result

    async def get_order(self, symbol, order_id=None, orig_client_order_id=None):
        return self.order_detail

    async def get_my_trades(self, symbol, limit=50, order_id=None):
        return self.trades

    async def cancel_order(self, symbol, order_id):
        return {}


class TestExchangeInfoBlock:
    async def test_buy_blocked_when_exchange_info_fails(self, db_tables):
        rest = _MockRest(exchange_info_error=RuntimeError("exchangeInfo down"))
        engine = ExecutionEngine(risk_manager=RiskManager(), rest_client=rest)
        sig = _make_signal(side=SignalSide.BUY, quantity=1.0)
        status, qty, *_ = await engine._execute_live(sig, "cid-1", None, 1.0)
        assert status == "REJECTED"
        assert qty == 0.0
        assert rest.create_calls == 0  # 未发交易所

    async def test_sell_allowed_when_exchange_info_fails(self, db_tables):
        rest = _MockRest(exchange_info_error=RuntimeError("exchangeInfo down"))
        engine = ExecutionEngine(risk_manager=RiskManager(), rest_client=rest)
        sig = _make_signal(side=SignalSide.SELL, quantity=1.0)
        status, *_ = await engine._execute_live(sig, "cid-1", None, 1.0)
        assert status == "FILLED"  # 减仓放行
        assert rest.create_calls == 1

    async def test_buy_allowed_when_exchange_info_ok(self, db_tables):
        rest = _MockRest()  # exchangeInfo 正常
        engine = ExecutionEngine(risk_manager=RiskManager(), rest_client=rest)
        sig = _make_signal(side=SignalSide.BUY, quantity=1.0)
        status, *_ = await engine._execute_live(sig, "cid-1", None, 1.0)
        assert status == "FILLED"
        assert rest.create_calls == 1
