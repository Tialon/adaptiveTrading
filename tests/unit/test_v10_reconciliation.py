"""V10: 权益对账 + 启动崩溃窗口恢复测试

- PositionReconciler.reconcile_account: 本地权益 vs 交易所权益, 超容差返回漂移。
- StartupReconciler.reconcile: 无交易所订单 ID / 自愈已成交 / 孤儿挂单 / API 异常。
"""

import pytest
from sqlalchemy import select

from at01_common.models import Order
from at50_execution.reconciliation import PositionReconciler
from at50_execution.startup_reconciler import StartupReconciler
from at50_execution.execution_executor import ExecutionEngine
from at60_risk.risk_manager import RiskManager


class FakeRest:
    """最小 REST 假实现, 只覆盖对账/启动恢复所需方法"""

    def __init__(self, account=None, open_orders=None, my_trades=None, order_detail=None,
                 account_error=None):
        self._account = account if account is not None else {"balances": []}
        self._open_orders = open_orders or []
        self._my_trades = my_trades or []
        self._order_detail = order_detail or {}
        self._account_error = account_error

    async def get_account(self):
        if self._account_error is not None:
            raise self._account_error
        return self._account

    async def get_open_orders(self, symbol):
        return self._open_orders

    async def get_my_trades(self, symbol, limit=100):
        return self._my_trades

    async def get_order(self, symbol, order_id):
        return self._order_detail


def _bal(asset, free, locked=0.0):
    return {"asset": asset, "free": str(free), "locked": str(locked)}


async def _insert_order(client_order_id, symbol="SOLUSDT", side="BUY", status="NEW",
                        exchange_order_id=None, is_paper=False, quantity=1.0, price=100.0):
    from at01_common.database import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        session.add(Order(
            client_order_id=client_order_id,
            exchange_order_id=exchange_order_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            status=status,
            is_paper=is_paper,
        ))
        await session.commit()


async def _get_order(client_order_id):
    from at01_common.database import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        return (
            await session.execute(
                select(Order).where(Order.client_order_id == client_order_id)
            )
        ).scalar_one_or_none()


class TestReconcileAccount:
    async def test_no_drift(self):
        rest = FakeRest(account={"balances": [
            _bal("USDT", 9900.0), _bal("SOL", 1.0),
        ]})
        r = PositionReconciler(rest_client=rest)
        drifts = await r.reconcile_account("SOLUSDT", local_equity=10000.0, last_price=100.0)
        assert drifts == []

    async def test_drift_over_tolerance(self):
        rest = FakeRest(account={"balances": [
            _bal("USDT", 9500.0), _bal("SOL", 1.0),
        ]})
        r = PositionReconciler(rest_client=rest)
        drifts = await r.reconcile_account("SOLUSDT", local_equity=10000.0, last_price=100.0)
        assert len(drifts) == 1
        d = drifts[0]
        assert d["type"] == "equity_drift"
        assert d["local"] == 10000.0
        assert d["exchange"] == pytest.approx(9600.0)  # 9500 + 1*100
        assert d["diff"] == pytest.approx(400.0)

    async def test_drift_within_tolerance(self):
        rest = FakeRest(account={"balances": [
            _bal("USDT", 9900.0), _bal("SOL", 1.0),
        ]})
        r = PositionReconciler(rest_client=rest)
        # 差 100(=1%) 在 2% 容差内 -> 不报
        drifts = await r.reconcile_account("SOLUSDT", local_equity=10000.0, last_price=100.0)
        assert drifts == []

    async def test_api_error(self):
        rest = FakeRest(account_error=RuntimeError("api down"))
        r = PositionReconciler(rest_client=rest)
        drifts = await r.reconcile_account("SOLUSDT", local_equity=10000.0, last_price=100.0)
        assert len(drifts) == 1
        assert drifts[0]["type"] == "api_error"

    async def test_zero_local_equity_skips(self):
        rest = FakeRest(account={"balances": [_bal("USDT", 0.0)]})
        r = PositionReconciler(rest_client=rest)
        drifts = await r.reconcile_account("SOLUSDT", local_equity=0.0, last_price=100.0)
        assert drifts == []


class TestStartupReconciler:
    async def test_no_exchange_id_unresolved(self, db_tables):
        await _insert_order("cid-1", exchange_order_id=None)
        rest = FakeRest(open_orders=[], my_trades=[])
        rec = StartupReconciler(rest_client=rest)
        diffs = await rec.reconcile("SOLUSDT")
        assert len(diffs) == 1
        assert diffs[0]["type"] == "no_exchange_id"

    async def test_open_order_kept_no_diff(self, db_tables):
        await _insert_order("cid-1", exchange_order_id="100")
        rest = FakeRest(open_orders=[{"orderId": "100"}], my_trades=[])
        rec = StartupReconciler(rest_client=rest)
        diffs = await rec.reconcile("SOLUSDT")
        assert diffs == []

    async def test_self_heal_filled(self, db_tables):
        await _insert_order("cid-1", exchange_order_id="100", side="BUY", status="NEW")
        rest = FakeRest(
            open_orders=[],
            my_trades=[{"orderId": "100"}],
            order_detail={"status": "FILLED", "executedQty": "1.0",
                          "cummulativeQuoteQty": "100.0"},
        )
        rm = RiskManager()
        engine = ExecutionEngine(risk_manager=rm)
        rec = StartupReconciler(rest_client=rest, execution_engine=engine, risk_manager=rm)
        diffs = await rec.reconcile("SOLUSDT")
        assert diffs == []  # 已确定性自愈, 无歧义

        row = await _get_order("cid-1")
        assert row.status == "FILLED"
        assert row.filled_quantity == pytest.approx(1.0)
        assert row.exchange_order_id == "100"

    async def test_self_heal_get_order_failure_not_canceled(self, db_tables):
        # V11.3 P1-6: 已成交订单的自愈取单失败 -> 计入歧义(急停), 绝不误标 CANCELED 丢成交
        await _insert_order("cid-1", exchange_order_id="100", side="BUY", status="NEW")

        class _FailGetOrderRest:
            async def get_open_orders(self, symbol):
                return []

            async def get_my_trades(self, symbol, limit=100):
                return [{"orderId": "100"}]

            async def get_order(self, symbol, order_id, **kw):
                raise RuntimeError("transient api failure")

        rec = StartupReconciler(rest_client=_FailGetOrderRest())
        diffs = await rec.reconcile("SOLUSDT")
        assert any(d["type"] == "ambiguous_order" for d in diffs)

        row = await _get_order("cid-1")
        assert row.status == "NEW"  # 不被误改

    async def test_orphan_exchange_order(self, db_tables):
        # 本地无单, 交易所却有挂单 -> 孤儿(歧义)
        rest = FakeRest(open_orders=[{"orderId": "999"}], my_trades=[])
        rec = StartupReconciler(rest_client=rest)
        diffs = await rec.reconcile("SOLUSDT")
        assert any(d["type"] == "orphan_exchange_order" for d in diffs)

    async def test_api_error(self, db_tables):
        class _BoomRest:
            async def get_open_orders(self, symbol):
                raise RuntimeError("api down")

        rec = StartupReconciler(rest_client=_BoomRest())
        diffs = await rec.reconcile("SOLUSDT")
        assert len(diffs) == 1
        assert diffs[0]["type"] == "api_error"

    async def test_no_rest_returns_empty(self, db_tables):
        rec = StartupReconciler(rest_client=None)
        assert await rec.reconcile("SOLUSDT") == []
