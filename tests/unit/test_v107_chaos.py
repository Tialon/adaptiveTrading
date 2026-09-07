"""V10.7(P1-g): Chaos 测试(故障注入 → 系统自愈/不漂移)

针对「超时 / 重复成交 / 部分成交 / DB 回滚 / 未知订单」五类典型故障,
验证执行链在异常下不静默漂移、恢复引擎可收敛、账务不重复记账:

1. 超时       —— 下单网络超时 -> UNKNOWN(不记账), 交易所真相收敛为 FILLED;
2. 重复成交   —— 同一成交重复投递 -> OrderFill 只落一行、持仓不重复记账;
3. 部分成交   —— 部分成交后撤单 -> 只记已成交部分(PARTIALLY_FILLED);
4. DB 回滚    —— 记账事务中途失败 -> 整体回滚 + RECOVERY_REQUIRED, 重建不重复;
5. 未知订单   —— 5xx 结果未明 -> UNKNOWN(不静默记账), 恢复收敛为 FILLED。
"""

import asyncio

import pytest
from sqlalchemy import func, select

from at01_common.database import AsyncSessionLocal
from at01_common.models import AccountLedger, Order, OrderFill, Position, PositionLot
from at20_market.market_rest_client import BinanceAPIError
from at50_execution.execution_executor import ExecutionEngine
from at50_execution.order_recovery import OrderRecoveryEngine
from at50_strategy.strategy_base import Signal, SignalSide
from at60_risk.risk_manager import RiskManager

SYMBOL = "SOLUSDT"

FILLED = {"orderId": "100", "status": "FILLED",
          "executedQty": "1.0", "cummulativeQuoteQty": "100.0", "price": "100.0"}
TRADE = {"id": 11, "orderId": "100", "price": "100.0", "qty": "1.0",
         "quoteQty": "100.0", "commission": "0", "commissionAsset": "USDT",
         "time": 1700000000000}


class FakeRest:
    """可配置故障注入的 REST 桩(create_order 可抛超时/5xx, get_order 可出队)"""

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
            raise asyncio.TimeoutError("network timeout")
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


async def _noop_sleep(*args, **kwargs):
    return None


async def _count(model) -> int:
    async with AsyncSessionLocal() as session:
        return (await session.execute(select(func.count()).select_from(model))).scalar()


async def _order(cid: str) -> Order:
    async with AsyncSessionLocal() as session:
        return (
            await session.execute(
                select(Order).where(Order.client_order_id == cid)
            )
        ).scalar_one()


def _signal(side: str, qty: float, price: float) -> Signal:
    return Signal(symbol=SYMBOL, strategy="trend", side=SignalSide(side),
                  price=price, quantity=qty)


def _live_engine(rest, rm: RiskManager) -> ExecutionEngine:
    e = ExecutionEngine(risk_manager=rm, rest_client=rest)
    e.is_paper = False
    return e


class TestChaos:
    # 1. 超时: 下单超时 -> UNKNOWN(不记账) -> 交易所真相收敛
    async def test_timeout_creates_unknown_then_recovers(self, db_tables):
        rm = RiskManager()
        engine = _live_engine(FakeRest(timeout=True), rm)
        result = await engine.execute(_signal("BUY", 1.0, 100.0))
        assert result is not None
        assert result["status"] == "UNKNOWN"
        assert result["fill_qty"] == 0.0
        cid = result["client_order_id"]
        # 超时未静默记账
        assert rm.positions.get(SYMBOL).quantity == 0.0
        assert await _count(Position) == 0

        # 交易所真相: 实际已成交 -> 恢复收敛
        engine.rest = FakeRest(order_detail=FILLED, trades=[TRADE])
        recovery = OrderRecoveryEngine(engine.rest, engine, rm)
        assert await recovery.recover(SYMBOL) == []

        o = await _order(cid)
        assert o.status == "FILLED"
        assert o.accounting_state == "RECOVERED"
        assert rm.positions.get(SYMBOL).quantity == 1.0
        assert await _count(PositionLot) == 1

    # 2. 重复成交: 同一成交重复投递 -> 只落一行、不重复记账
    async def test_duplicate_fill_ingested_once(self, db_tables):
        rm = RiskManager()
        engine = _live_engine(FakeRest(order_detail=FILLED, trades=[TRADE, dict(TRADE)]), rm)
        result = await engine.execute(_signal("BUY", 1.0, 100.0))
        assert result["status"] == "FILLED"
        assert await _count(OrderFill) == 1  # 幂等去重
        assert rm.positions.get(SYMBOL).quantity == 1.0  # 不重复记账

    # 3. 部分成交: 部分成交后撤单 -> 只记已成交部分
    async def test_partial_fill_accounts_partial(self, db_tables, monkeypatch):
        partial = {"orderId": "100", "status": "PARTIALLY_FILLED",
                   "executedQty": "0.5", "cummulativeQuoteQty": "50.0"}
        canceled_with_fill = {"orderId": "100", "status": "CANCELED",
                              "executedQty": "0.5", "cummulativeQuoteQty": "50.0"}
        half_trade = {"id": 11, "orderId": "100", "price": "100.0", "qty": "0.5",
                      "quoteQty": "50.0", "commission": "0", "commissionAsset": "USDT",
                      "time": 1700000000000}
        monkeypatch.setattr(asyncio, "sleep", _noop_sleep)  # 跳过成交轮询等待

        rm = RiskManager()
        engine = _live_engine(FakeRest(order_queue=[partial, canceled_with_fill],
                                       trades=[half_trade]), rm)
        result = await engine.execute(_signal("BUY", 1.0, 100.0))
        assert result["status"] == "PARTIALLY_FILLED"
        assert result["fill_qty"] == pytest.approx(0.5)
        assert rm.positions.get(SYMBOL).quantity == pytest.approx(0.5)
        o = await _order(result["client_order_id"])
        assert o.filled_quantity == pytest.approx(0.5)
        assert o.status == "PARTIALLY_FILLED"

    # 4. DB 回滚: 记账事务中途失败 -> 整体回滚 + 重建不重复记账
    async def test_db_rollback_then_rebuild_no_double_count(self, db_tables, monkeypatch):
        rm = RiskManager()
        engine = _live_engine(FakeRest(order_detail=FILLED, trades=[TRADE]), rm)

        async def _boom(*args, **kwargs):
            raise RuntimeError("lot insert boom")

        # PositionLot 落库失败(事务内) -> 整体回滚
        monkeypatch.setattr(engine.lot_tracker, "_insert_lot", _boom)

        result = await engine.execute(_signal("BUY", 1.0, 100.0))
        assert result["status"] == "RECOVERY_REQUIRED"
        cid = result["client_order_id"]
        assert rm.kill_switch.is_armed is True

        # 账本镜像整体回滚(无部分镜像)
        assert await _count(Position) == 0
        assert await _count(PositionLot) == 0
        assert await _count(AccountLedger) == 0

        # 内存权威态未丢(持仓已 bump + lot 已含, id=None)
        assert rm.positions.get(SYMBOL).quantity == 1.0
        assert engine._find_lot(SYMBOL, cid) is not None

        # 账务重建: 只补 DB 镜像, 不重复变更内存
        assert await engine.rebuild_buy_accounting(
            symbol=SYMBOL, client_order_id=cid, exchange_order_id="100",
            fill_qty=1.0, fill_price=100.0, fee=0.0,
        ) == "recovered"

        assert rm.positions.get(SYMBOL).quantity == 1.0  # 不重复记账
        assert await _count(Position) == 1
        assert await _count(PositionLot) == 1
        o = await _order(cid)
        assert o.accounting_state == "RECOVERED"

    # 5. 未知订单: 5xx 结果未明 -> UNKNOWN(不静默记账) -> 恢复收敛
    async def test_ambiguous_5xx_creates_unknown_then_recovers(self, db_tables):
        rm = RiskManager()
        engine = _live_engine(FakeRest(five_xx=True), rm)
        result = await engine.execute(_signal("BUY", 1.0, 100.0))
        assert result["status"] == "UNKNOWN"
        cid = result["client_order_id"]
        # 结果未明时绝不静默记账
        assert rm.positions.get(SYMBOL).quantity == 0.0
        assert await _count(Position) == 0

        # 交易所真相: 已成交 -> 恢复收敛
        engine.rest = FakeRest(order_detail=FILLED, trades=[TRADE])
        recovery = OrderRecoveryEngine(engine.rest, engine, rm)
        assert await recovery.recover(SYMBOL) == []

        o = await _order(cid)
        assert o.status == "FILLED"
        assert o.accounting_state == "RECOVERED"
        assert rm.positions.get(SYMBOL).quantity == 1.0
