"""V12.1(P1): 执行/恢复链路可空值契约的 fail-closed 测试

覆盖三条必须补测的可空值契约:
1. REST/execution 依赖缺失时, 恢复/启动对账不 AttributeError, 保持 fail-closed(禁开仓);
2. exchange_order_id=None 的恢复订单不触发成交明细查询、不伪造成交;
3. 空/非法时间戳与金额进入对账得到可审计的不一致结果, 不崩溃。
"""

from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import func, select

from at01_common.database import AsyncSessionLocal
from at01_common.models import Order, Position
from at60_execution.cross_reconciler import CrossReconciler
from at60_execution.exchange_truth_reconciler import ExchangeTruthReconciler
from at60_execution.execution_executor import ExecutionEngine
from at60_execution.order_recovery import OrderRecoveryEngine
from at60_execution.startup_reconciler import StartupReconciler
from at50_risk.risk_manager import RiskManager

SYMBOL = "SOLUSDT"


class _FakeRest:
    """最小 REST 桩(仅覆盖订单恢复/启动对账需要的 get_order / get_my_trades)"""

    def __init__(self, orders=None, trades=None):
        self.orders = orders or {}
        self.trades = trades or {}

    async def get_order(self, symbol, exchange_order_id=None, orig_client_order_id=None):
        if orig_client_order_id is not None:
            return self.orders.get(orig_client_order_id)
        return self.orders.get(str(exchange_order_id))

    async def get_my_trades(self, symbol, order_id=None, limit=None):
        return self.trades.get(str(order_id), [])


def _engine():
    e = ExecutionEngine(risk_manager=RiskManager())
    e.is_paper = False
    e.rest = _FakeRest()
    return e


async def _insert_order(cid, *, side="BUY", status="UNKNOWN", accounting_state="OK",
                        filled=0.0, avg=None, eid=None):
    async with AsyncSessionLocal() as session:
        session.add(Order(
            client_order_id=cid, symbol=SYMBOL, side=side,
            order_type="MARKET", quantity=1.0, status=status,
            is_paper=False, accounting_state=accounting_state,
            filled_quantity=filled, avg_fill_price=avg, exchange_order_id=eid,
        ))
        await session.commit()


async def _count(model) -> int:
    async with AsyncSessionLocal() as session:
        return (await session.execute(select(func.count()).select_from(model))).scalar()


# ---------------------------------------------------------------------------
# 1. REST/execution 依赖缺失 -> fail-closed, 不 AttributeError
# ---------------------------------------------------------------------------

class TestMissingDependencyFailClosed:
    async def test_recovery_execution_none_returns_empty(self, db_tables):
        """execution=None 时 recover() 顶层守卫直接返回空, 不崩溃、不误收敛"""
        e = _engine()
        await _insert_order("cid-1", side="BUY", status="UNKNOWN")
        recovery = OrderRecoveryEngine(e.rest, None, e.risk)
        assert await recovery.recover(SYMBOL) == []

    async def test_recover_status_execution_none_no_attrerror(self, db_tables):
        """execution=None 直达 _recover_status: 新守卫返回 False(未解决)而非 AttributeError"""
        e = _engine()
        recovery = OrderRecoveryEngine(e.rest, None, e.risk)
        ok = await recovery._recover_status(SYMBOL, {
            "client_order_id": "cid-2", "side": "BUY", "status": "UNKNOWN",
        })
        assert ok is False

    async def test_mark_canceled_execution_none_no_attrerror(self, db_tables):
        """execution=None 直达 _mark_canceled: 新守卫直接返回, 不触碰 trade_sm/events"""
        e = _engine()
        recovery = OrderRecoveryEngine(e.rest, None, e.risk)
        await recovery._mark_canceled(SYMBOL, {"client_order_id": "cid-3", "side": "BUY"})

    async def test_startup_self_heal_rest_none_returns_false(self, db_tables):
        """rest=None 直达 _self_heal_filled: 返回 False(计入未解决), 不误标撤单"""
        sr = StartupReconciler(rest_client=None, execution_engine=None, risk_manager=None)
        ok = await sr._self_heal_filled(SYMBOL, {
            "client_order_id": "cid-4", "side": "BUY",
        }, "123")
        assert ok is False

    async def test_startup_resolve_unknown_rest_none_returns_false(self, db_tables):
        """rest=None 直达 _resolve_unknown: 返回 False(计入未解决, 禁开仓)"""
        sr = StartupReconciler(rest_client=None, execution_engine=None, risk_manager=None)
        ok = await sr._resolve_unknown(SYMBOL, {
            "client_order_id": "cid-5", "side": "BUY", "status": "UNKNOWN",
        })
        assert ok is False


# ---------------------------------------------------------------------------
# 2. exchange_order_id=None 的恢复订单不触发查询、不伪造成交
# ---------------------------------------------------------------------------

class TestRecoveredFillNoExchangeId:
    async def test_no_eid_skips_fill_ingest_and_accounts(self, db_tables):
        """exchange_order_id=None 时不得调用 get_my_trades(会退化为无过滤全量查询)"""
        e = _engine()
        cid = "cid-noeid"
        await _insert_order(cid, side="BUY", status="UNKNOWN")

        async def _boom_get_my_trades(symbol, order_id=None, limit=None):
            raise AssertionError("exchange_order_id=None 时不得调用 get_my_trades")

        e.rest.get_my_trades = _boom_get_my_trades

        r = await e.apply_recovered_fill(
            symbol=SYMBOL, side="BUY", client_order_id=cid,
            exchange_order_id=None, fill_qty=1.0, fill_price=100.0, fee=0.0,
        )
        assert r == "filled"  # 用订单级成交数据记账, 未伪造交易所成交
        assert e.risk.positions.get(SYMBOL).quantity == 1.0
        assert await _count(Position) == 1


# ---------------------------------------------------------------------------
# 3. 空/非法时间戳与金额 -> 可审计不一致, 不崩溃
# ---------------------------------------------------------------------------

class TestInvalidTimestampAndAmount:
    def test_derive_start_ms_rejects_invalid_timestamps(self):
        """空/非 datetime 的 created_at 被拒绝, 只取合法 datetime 的最早值"""
        r = ExchangeTruthReconciler()
        local = [
            {"created_at": None},
            {"created_at": "not-a-datetime"},
            {"created_at": datetime(2026, 9, 3, 0, 0, 0)},
            {"created_at": datetime(2026, 9, 1, 0, 0, 0)},
        ]
        ms = r._derive_start_ms(local, 900.0)
        expected = int(datetime(2026, 9, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp() * 1000) - 60000
        assert ms == expected

    def test_derive_start_ms_no_valid_timestamp_falls_back(self):
        """全部空/非法时回退 now-window, 不崩溃"""
        r = ExchangeTruthReconciler()
        ms = r._derive_start_ms([{"created_at": None}, {"created_at": "garbage"}], 900.0)
        assert isinstance(ms, int)

    def test_cross_reconciler_m_accepts_non_numeric(self):
        """非数值字段(方向字符串/None)也能构造可审计差异, 不因类型崩溃"""
        r = CrossReconciler()
        order = SimpleNamespace(symbol=SYMBOL, client_order_id="cid-m")
        d = r._m(order, "fill_side_mismatch", "BUY", None)
        assert d["type"] == "fill_side_mismatch"
        assert d["expected"] == "BUY"
        assert d["actual"] is None
