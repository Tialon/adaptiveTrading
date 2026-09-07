"""V10.1: Order→Fill→Ledger 链单元测试

- UNKNOWN 状态迁移合法性
- 异常分类(4xx→REJECTED / 5xx、网络→UNKNOWN)
- OrderIntent 幂等(DB 唯一键)
- OrderFill 逐笔落库 + 幂等摄入
- 手续费合成(fee_quote) + AccountLedger 落 commission
"""

import asyncio

import pytest
from sqlalchemy import select

from at20_market.market_rest_client import BinanceAPIError
from at50_execution.execution_executor import ExecutionEngine, _compute_fill_metrics
from at50_execution.execution_state import OrderState
from at50_strategy.strategy_base import Signal, SignalSide
from at60_risk.risk_manager import RiskManager


# ---------- UNKNOWN 状态 ----------

class TestUnknownState:
    def test_submit_to_unknown_allowed(self):
        assert OrderState.SUBMIT.can_transition(OrderState.UNKNOWN)

    def test_unknown_reconcile_to_real_states(self):
        for target in (
            OrderState.OPEN,
            OrderState.PARTIAL_FILL,
            OrderState.FILLED,
            OrderState.CANCELED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
        ):
            assert OrderState.UNKNOWN.can_transition(target), target

    def test_unknown_not_terminal(self):
        assert not OrderState.UNKNOWN.terminal

    def test_filled_terminal_blocks_transition(self):
        assert OrderState.FILLED.terminal
        assert not OrderState.FILLED.can_transition(OrderState.CANCELED)


# ---------- 异常分类 ----------

class _MockRest:
    """最小 REST 假实现, 覆盖下单/查单/成交/撤单"""

    def __init__(self):
        self.create_errors: list = []  # create_order 依次抛出的异常(空=正常)
        self.create_result = {"orderId": "100"}
        self.order_detail = {"status": "FILLED", "executedQty": "1.0",
                             "cummulativeQuoteQty": "100.0"}
        self.trades: list = []

    async def create_order(self, **kw):
        if self.create_errors:
            err = self.create_errors.pop(0)
            if isinstance(err, Exception):
                raise err
        return self.create_result

    async def get_order(self, symbol, order_id=None, orig_client_order_id=None):
        return self.order_detail

    async def get_my_trades(self, symbol, limit=50, order_id=None):
        return self.trades

    async def cancel_order(self, symbol, order_id):
        return {}


def _make_signal(**kw) -> Signal:
    base = dict(symbol="BTCUSDT", strategy="grid", side=SignalSide.BUY,
                price=100.0, quantity=1.0)
    base.update(kw)
    return Signal(**base)


class TestExceptionClassification:
    async def test_4xx_rejected_no_retry(self, db_tables):
        rest = _MockRest()
        rest.create_errors = [BinanceAPIError(400, -1100, "bad quantity")]
        engine = ExecutionEngine(risk_manager=RiskManager(), rest_client=rest)
        status, qty, _price, _fee, eid = await engine._execute_live(
            _make_signal(), "cid-1", None, 1.0
        )
        assert status == "REJECTED"
        assert qty == 0.0
        assert eid is None
        # 明确拒绝不重试: create_errors 应耗尽(仅一次)
        assert rest.create_errors == []

    async def test_5xx_unknown(self, db_tables):
        rest = _MockRest()
        rest.create_errors = [
            BinanceAPIError(500, -1000, "internal"),
            BinanceAPIError(500, -1000, "internal"),
        ]
        rest.order_detail = {}  # 反查无单
        engine = ExecutionEngine(risk_manager=RiskManager(), rest_client=rest)
        status, qty, _price, _fee, eid = await engine._execute_live(
            _make_signal(), "cid-1", None, 1.0
        )
        assert status == "UNKNOWN"
        assert qty == 0.0
        assert eid is None

    async def test_network_timeout_unknown(self, db_tables):
        rest = _MockRest()
        rest.create_errors = [asyncio.TimeoutError(), asyncio.TimeoutError()]
        rest.order_detail = {}
        engine = ExecutionEngine(risk_manager=RiskManager(), rest_client=rest)
        status, qty, _price, _fee, eid = await engine._execute_live(
            _make_signal(), "cid-1", None, 1.0
        )
        assert status == "UNKNOWN"
        assert eid is None


# ---------- OrderIntent 幂等 ----------

class TestOrderIntentIdempotency:
    async def test_register_intent_same_key_blocked(self, db_tables):
        engine = ExecutionEngine(risk_manager=RiskManager())
        sig = _make_signal(quantity=1.0)
        key = engine._idempotency_key(sig)
        assert await engine._register_intent(sig, key) is True
        assert await engine._register_intent(sig, key) is False  # 同键重复 -> 拦截

    async def test_different_quantity_not_conflicting(self, db_tables):
        engine = ExecutionEngine(risk_manager=RiskManager())
        sig_a = _make_signal(quantity=1.0)
        sig_b = _make_signal(quantity=0.5)
        key_a = engine._idempotency_key(sig_a)
        key_b = engine._idempotency_key(sig_b)
        assert key_a != key_b  # 评审 Signal A(1)/B(0.5) 不再误判
        assert await engine._register_intent(sig_a, key_a) is True
        assert await engine._register_intent(sig_b, key_b) is True


# ---------- OrderFill ----------

class TestOrderFill:
    async def test_record_fills_idempotent(self, db_tables):
        from at01_common.database import AsyncSessionLocal
        from at01_common.models import OrderFill

        engine = ExecutionEngine(risk_manager=RiskManager())
        fills = [
            {"id": 11, "orderId": "100", "price": "99.5", "qty": "0.5",
             "quoteQty": "49.75", "commission": "0.05", "commissionAsset": "USDT",
             "time": 1700000000000},
            {"id": 12, "orderId": "100", "price": "100.5", "qty": "0.5",
             "quoteQty": "50.25", "commission": "0.0005", "commissionAsset": "SOL",
             "time": 1700000001000},
        ]
        args = dict(order_id=None, client_order_id="cid-1", exchange_order_id="100",
                    symbol="SOLUSDT", side="BUY")
        assert await engine._record_fills(fills=fills, **args) == 2
        # 重复摄入同一批成交 -> 不重复落库(幂等)
        assert await engine._record_fills(fills=fills, **args) == 0

        async with AsyncSessionLocal() as session:
            rows = (await session.execute(select(OrderFill))).scalars().all()
            assert len(rows) == 2
            assert {r.exchange_trade_id for r in rows} == {11, 12}


# ---------- 手续费合成 + 账本 ----------

class TestFeeSynthesis:
    def test_compute_fill_metrics_mixed_assets(self):
        fills = [
            {"qty": "0.5", "quoteQty": "49.75", "price": "99.5",
             "commission": "0.05", "commissionAsset": "USDT"},
            {"qty": "0.5", "quoteQty": "50.25", "price": "100.5",
             "commission": "0.001", "commissionAsset": "SOL"},
        ]
        avg, fee = _compute_fill_metrics("SOLUSDT", fills)
        assert avg == pytest.approx((49.75 + 50.25) / (0.5 + 0.5))
        # fee = 0.05(USDT 计价) + 0.001 * 100.5(SOL 计价×成交价)
        assert fee == pytest.approx(0.05 + 0.001 * 100.5)

    async def test_account_ledger_records_commission(self, db_tables):
        from at01_common.database import AsyncSessionLocal
        from at01_common.models import AccountLedger
        from at60_risk.risk_account_ledger import AccountLedgerWriter

        w = AccountLedgerWriter()
        ok = await w.record(
            ts=1700000000000, symbol="SOLUSDT", bucket="trade", side="BUY",
            cash_before=10000.0, cash_after=9899.95,
            pos_before=0.0, pos_after=1.0,
            commission=0.05, commission_asset="USDT",
        )
        assert ok is True
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(select(AccountLedger))).scalars().all()
            assert len(rows) == 2  # USDT + SOL 两行
            assert all(r.commission == pytest.approx(0.05) for r in rows)
            assert all(r.commission_asset == "USDT" for r in rows)
