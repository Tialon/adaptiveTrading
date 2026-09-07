"""V10.2: 完整对账盲区 + SUBMITTING/ExecutionAttempt 测试

- SUBMITTING 状态迁移合法性
- _execute_live 下单前落 SUBMITTING + 每次尝试落 ExecutionAttempt(attempt_no/outcome)
- reconcile_live 反向检出 EXCHANGE_ONLY
- startup_reconciler 对 SUBMITTING(无交易所 ID)按 clientOrderId 反查收敛
"""

import pytest
from sqlalchemy import select

from at20_market.market_rest_client import BinanceAPIError
from at50_execution.execution_executor import ExecutionEngine
from at50_execution.execution_state import OrderState
from at50_execution.reconciliation import PositionReconciler
from at50_execution.startup_reconciler import StartupReconciler
from at50_strategy.strategy_base import Signal, SignalSide
from at60_risk.risk_manager import RiskManager


def _make_signal(**kw) -> Signal:
    base = dict(symbol="BTCUSDT", strategy="grid", side=SignalSide.BUY,
                price=100.0, quantity=1.0)
    base.update(kw)
    return Signal(**base)


async def _insert_order(client_order_id, status="NEW", exchange_order_id=None,
                        side="BUY", is_paper=False):
    from at01_common.database import AsyncSessionLocal
    from at01_common.models import Order

    async with AsyncSessionLocal() as session:
        session.add(Order(
            client_order_id=client_order_id,
            exchange_order_id=exchange_order_id,
            symbol="SOLUSDT", side=side, quantity=1.0, price=100.0,
            status=status, is_paper=is_paper,
        ))
        await session.commit()


async def _get_order(client_order_id):
    from at01_common.database import AsyncSessionLocal
    from at01_common.models import Order

    async with AsyncSessionLocal() as session:
        return (await session.execute(
            select(Order).where(Order.client_order_id == client_order_id)
        )).scalar_one_or_none()


async def _get_attempts(client_order_id):
    from at01_common.database import AsyncSessionLocal
    from at01_common.models import ExecutionAttempt

    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(ExecutionAttempt)
            .where(ExecutionAttempt.client_order_id == client_order_id)
            .order_by(ExecutionAttempt.attempt_no)
        )).scalars().all()
        return list(rows)


class _MockRest:
    """最小 REST 假实现, 覆盖下单/查单/成交/撤单"""

    def __init__(self):
        self.create_errors: list = []
        self.create_result = {"orderId": "100"}
        self.order_detail = {"status": "FILLED", "executedQty": "1.0",
                             "cummulativeQuoteQty": "100.0"}
        self.trades: list = []
        self.status_at_create = None  # create_order 瞬间读取的本地订单状态

    async def create_order(self, **kw):
        # 在下单瞬间读取本地订单状态(验证已落 SUBMITTING)
        from at01_common.database import AsyncSessionLocal
        from at01_common.models import Order

        async with AsyncSessionLocal() as session:
            row = (await session.execute(
                select(Order).where(Order.client_order_id == kw["new_client_order_id"])
            )).scalar_one_or_none()
            if row is not None:
                self.status_at_create = row.status
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


# ---------- SUBMITTING 状态 ----------

class TestSubmittingState:
    def test_create_to_submitting(self):
        assert OrderState.CREATE.can_transition(OrderState.SUBMITTING)

    def test_submitting_to_real_states(self):
        for target in (OrderState.SUBMIT, OrderState.OPEN, OrderState.FILLED,
                       OrderState.PARTIAL_FILL, OrderState.REJECTED,
                       OrderState.EXPIRED, OrderState.UNKNOWN, OrderState.CANCELED):
            assert OrderState.SUBMITTING.can_transition(target), target

    def test_submitting_not_terminal(self):
        assert not OrderState.SUBMITTING.terminal


# ---------- SUBMITTING 埋点 + ExecutionAttempt ----------

class TestExecutionAttempt:
    async def test_submitting_marked_before_create(self, db_tables):
        await _insert_order("cid-1", status="NEW")
        rest = _MockRest()  # 下单成功
        engine = ExecutionEngine(risk_manager=RiskManager(), rest_client=rest)
        status, *_ = await engine._execute_live(_make_signal(), "cid-1", None, 1.0)
        assert status == "FILLED"
        assert rest.status_at_create == "SUBMITTING"

    async def test_success_attempt(self, db_tables):
        await _insert_order("cid-1", status="NEW")
        rest = _MockRest()
        engine = ExecutionEngine(risk_manager=RiskManager(), rest_client=rest)
        status, *_ = await engine._execute_live(_make_signal(), "cid-1", None, 1.0)
        assert status == "FILLED"
        attempts = await _get_attempts("cid-1")
        assert len(attempts) == 1
        assert attempts[0].attempt_no == 1
        assert attempts[0].outcome == "success"
        assert attempts[0].exchange_order_id == "100"

    async def test_4xx_rejected_attempt(self, db_tables):
        await _insert_order("cid-1", status="NEW")
        rest = _MockRest()
        rest.create_errors = [BinanceAPIError(400, -1100, "bad quantity")]
        engine = ExecutionEngine(risk_manager=RiskManager(), rest_client=rest)
        status, *_ = await engine._execute_live(_make_signal(), "cid-1", None, 1.0)
        assert status == "REJECTED"
        attempts = await _get_attempts("cid-1")
        assert len(attempts) == 1
        assert attempts[0].attempt_no == 1
        assert attempts[0].outcome == "rejected"

    async def test_5xx_ambiguous_then_retry(self, db_tables):
        await _insert_order("cid-1", status="NEW")
        rest = _MockRest()
        rest.create_errors = [
            BinanceAPIError(500, -1000, "internal"),
            BinanceAPIError(500, -1000, "internal"),
        ]
        rest.order_detail = {}  # 反查无单
        engine = ExecutionEngine(risk_manager=RiskManager(), rest_client=rest)
        status, *_ = await engine._execute_live(_make_signal(), "cid-1", None, 1.0)
        assert status == "UNKNOWN"
        attempts = await _get_attempts("cid-1")
        assert [a.attempt_no for a in attempts] == [1, 2]
        assert all(a.outcome == "ambiguous" for a in attempts)


# ---------- 对账 EXCHANGE_ONLY ----------

class _Pos:
    def __init__(self, quantity: float):
        self.quantity = quantity


class _AccountRest:
    def __init__(self, balances):
        self._balances = balances

    async def get_account(self):
        return {"balances": self._balances}


class TestReconcileExchangeOnly:
    async def test_exchange_only_detected(self):
        rest = _AccountRest([
            {"asset": "SOL", "free": "12.3", "locked": "0.0"},
            {"asset": "USDT", "free": "5000.0", "locked": "0.0"},
        ])
        rec = PositionReconciler(rest_client=rest)
        diffs = await rec.reconcile_live({"SOLUSDT": _Pos(0.0)})
        types = {d["type"] for d in diffs}
        assert "exchange_only" in types
        eo = next(d for d in diffs if d["type"] == "exchange_only")
        assert eo["symbol"] == "SOLUSDT"
        assert eo["exchange"] == pytest.approx(12.3)

    async def test_no_diff_when_matched(self):
        rest = _AccountRest([
            {"asset": "SOL", "free": "1.0", "locked": "0.0"},
            {"asset": "USDT", "free": "5000.0", "locked": "0.0"},
        ])
        rec = PositionReconciler(rest_client=rest)
        diffs = await rec.reconcile_live({"SOLUSDT": _Pos(1.0)})
        assert diffs == []

    async def test_mismatch_local_only(self):
        # 本地有持仓、交易所无 -> mismatch(本地单侧)
        rest = _AccountRest([
            {"asset": "USDT", "free": "5000.0", "locked": "0.0"},
        ])
        rec = PositionReconciler(rest_client=rest)
        diffs = await rec.reconcile_live({"SOLUSDT": _Pos(2.0)})
        assert any(d["type"] == "mismatch" for d in diffs)


# ---------- startup 对账 SUBMITTING 收敛 ----------

class TestStartupSubmitting:
    async def test_submitting_resolved_via_client_id(self, db_tables):
        await _insert_order("cid-1", status="SUBMITTING", exchange_order_id=None)
        rest = _MockRest()
        rest.order_detail = {"status": "FILLED", "executedQty": "1.0",
                             "cummulativeQuoteQty": "100.0"}

        class _StartupRest:
            async def get_open_orders(self, symbol):
                return []

            async def get_my_trades(self, symbol, limit=100):
                return []

            async def get_order(self, symbol, order_id=None, orig_client_order_id=None):
                return {"orderId": "100", "status": "FILLED",
                        "executedQty": "1.0", "cummulativeQuoteQty": "100.0"}

        rm = RiskManager()
        engine = ExecutionEngine(risk_manager=rm)
        rec = StartupReconciler(rest_client=_StartupRest(),
                                execution_engine=engine, risk_manager=rm)
        diffs = await rec.reconcile("SOLUSDT")
        assert diffs == []
        row = await _get_order("cid-1")
        assert row.status == "FILLED"
        assert row.exchange_order_id == "100"
