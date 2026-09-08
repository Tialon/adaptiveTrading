"""V11.4 P0-5 崩溃 / 重启浸泡测试(crash/restart soak)。

目标: 从「测试证明正确」迈向「长期运行证明正确」。无人值守系统崩溃窗口
(下单后、成交确认前进程死掉)是漂移与重复记账的高发区。本片钉住五类重启语义:

1. 崩溃后成交(下单已成交但本地未记账): StartupReconciler 确定性自愈 ->
   `apply_recovered_fill` 完整记账, 重启后持仓/镜像/财务不变量成立;
2. 崩溃后已记账(记账已落、仅内存丢失): 重启(新 RiskManager + 引擎 + load_from_db)
   幂等 —— 不再重复记账, 不变量成立;
3. 下单前崩溃(无交易所订单 ID): 无法匹配 -> 未解决差异冻结, **绝不伪造成交**;
4. 交易所孤儿挂单(本地无匹配): 未解决差异冻结, 不自动改账;
5. 连续多个崩溃窗口自我修复: 累积持仓单调正确, 每窗后不变量成立。

关键语义(与 test_v135 / test_v107 互补):
- 纯内存状态机(RiskStateMachine)重启回 NORMAL, 但急停(KillSwitch)持久化兜底(见 test_v135);
- 崩溃窗口成交靠 StartupReconciler 拉交易所真相 + apply_recovered_fill 重放记账(见 test_v107);
- 本片聚焦「崩溃 -> 重启 -> 内存态重建(load_from_db) -> 对账自愈」的端到端闭环 + 不变量。
"""

import pytest
from sqlalchemy import func, select

from at01_common.database import AsyncSessionLocal
from at01_common.models import Order, Position, PositionLot
from at50_execution.startup_reconciler import StartupReconciler
from at60_risk.risk_manager import RiskManager
from tests.long_running._harness import (
    FakeRest,
    _count,
    _filled,
    _live_engine,
    _signal,
    _trade,
    assert_financial_invariants,
)

SYMBOL = "SOLUSDT"


class _CrashRest:
    """启动对账用的最小交易所桩: 挂单 / 成交历史 / 查单三接口。

    - `get_open_orders`: 交易所当前挂单(崩溃窗口后仍挂单或孤儿单);
    - `get_my_trades(symbol, limit=N)`: 崩溃窗口成交历史(reconciler 路由自愈用);
    - `get_my_trades(symbol, order_id=...)`: 每单成交明细查询返回空, 使
      `apply_recovered_fill` 走订单级成交数据(均价/fee 由订单详情决定, 确定性);
    - `get_order`: 按交易所 ID 或 clientOrderId 查单终态 / 成交详情。
    """

    def __init__(self, *, open_orders=None, trades=None, order_details=None):
        self.open_orders = list(open_orders or [])
        self.trades = list(trades or [])
        self.order_details = order_details or {}

    async def get_open_orders(self, symbol):
        return self.open_orders

    async def get_my_trades(self, symbol, limit=100, order_id=None):
        if order_id is not None:
            return []  # 每单明细查询为空, 走订单级成交数据(fee=0, 确定性)
        return self.trades

    async def get_order(self, symbol, exchange_order_id=None, orig_client_order_id=None):
        if orig_client_order_id is not None:
            return self.order_details.get(orig_client_order_id)
        return self.order_details.get(str(exchange_order_id))


async def _insert_order(cid, *, side="BUY", status="NEW", accounting_state="OK",
                        filled=0.0, avg=None, eid=None):
    """插入一条本地实盘订单(崩溃窗口前已落库, 但可能未记账/无交易所 ID)。"""
    async with AsyncSessionLocal() as session:
        session.add(Order(
            client_order_id=cid, symbol=SYMBOL, side=side,
            order_type="MARKET", quantity=1.0, status=status,
            is_paper=False, accounting_state=accounting_state,
            filled_quantity=filled, avg_fill_price=avg, exchange_order_id=eid,
        ))
        await session.commit()


def _restart(rest) -> tuple[RiskManager, "StartupReconciler", object]:
    """模拟进程重启: 全新 RiskManager + 引擎, 从 DB 重建内存态, 装配启动对账器。"""
    rm = RiskManager()
    engine = _live_engine(rest, rm)
    return rm, StartupReconciler(rest, engine, rm), engine


async def _reload_in_memory(rm, engine) -> None:
    """重启后从 DB 重建内存权威态(持仓 + 开仓 lot), 否则内存空仓会破坏不变量。"""
    await rm.positions.load_from_db()
    await engine.lot_tracker.load_from_db()


class TestCrashAfterFillSelfHeal:
    async def test_crash_after_fill_self_heals_and_invariants_hold(self, db_tables):
        """崩溃窗口: 下单已成交(交易所 FILLED)但本地未记账 -> 启动自愈 + 不变量成立。"""
        await _insert_order("crash-fill", side="BUY", status="NEW", eid="900")

        rest = _CrashRest(
            open_orders=[],  # 已成交, 交易所无挂单
            trades=[{"orderId": "900", "id": 1, "price": "100.0", "qty": "1.0",
                     "quoteQty": "100.0", "commission": "0", "commissionAsset": "USDT",
                     "time": 1700000000000}],
            order_details={"900": {"orderId": "900", "status": "FILLED",
                                   "executedQty": "1.0", "cummulativeQuoteQty": "100.0"}},
        )
        rm, rec, engine = _restart(rest)
        await _reload_in_memory(rm, engine)

        unresolved = await rec.reconcile(SYMBOL)
        assert unresolved == []

        # 自愈: 完整记账(内存 + DB 镜像)
        assert rm.positions.get(SYMBOL).quantity == pytest.approx(1.0)
        assert await _count(Position) == 1
        assert await _count(PositionLot) == 1
        await assert_financial_invariants(engine, rm, 100.0, check_paper=False)

    async def test_crash_after_fill_heals_partial_fill(self, db_tables):
        """部分成交(executedQty<quantity)也按交易所真相记账, 不静默丢弃、不多记。"""
        await _insert_order("crash-partial", side="BUY", status="SUBMITTING",
                            eid="901")

        rest = _CrashRest(
            open_orders=[],
            trades=[{"orderId": "901", "id": 2, "price": "100.0", "qty": "0.5",
                     "quoteQty": "50.0", "commission": "0", "commissionAsset": "USDT",
                     "time": 1700000000000}],
            order_details={"901": {"orderId": "901", "status": "FILLED",
                                   "executedQty": "0.5", "cummulativeQuoteQty": "50.0"}},
        )
        rm, rec, engine = _restart(rest)
        await _reload_in_memory(rm, engine)

        assert await rec.reconcile(SYMBOL) == []
        assert rm.positions.get(SYMBOL).quantity == pytest.approx(0.5)
        assert await _count(Position) == 1
        await assert_financial_invariants(engine, rm, 100.0, check_paper=False)

    async def test_submitting_without_eid_resolves_by_client_order_id(self, db_tables):
        """下单后、交易所 ID 落库前崩溃(SUBMITTING 无 eid): 按 clientOrderId 反查自愈。"""
        await _insert_order("crash-submitting", side="BUY", status="SUBMITTING", eid=None)

        filled = {"orderId": "902", "status": "FILLED",
                  "executedQty": "1.0", "cummulativeQuoteQty": "100.0"}
        rest = _CrashRest(
            open_orders=[],
            trades=[],  # 无成交历史(靠 clientOrderId 反查)
            order_details={
                "crash-submitting": filled,  # orig_client_order_id 反查
                "902": filled,               # _self_heal_filled 按 eid 再查一次
            },
        )
        rm, rec, engine = _restart(rest)
        await _reload_in_memory(rm, engine)

        assert await rec.reconcile(SYMBOL) == []
        assert rm.positions.get(SYMBOL).quantity == pytest.approx(1.0)
        assert await _count(Position) == 1
        await assert_financial_invariants(engine, rm, 100.0, check_paper=False)


class TestCrashAfterAccountingIdempotent:
    async def test_restart_after_accounting_is_idempotent(self, db_tables):
        """记账已落、仅内存丢失 -> 重启 load_from_db + 对账(无挂单)不重复记账。"""
        # 先完成一笔完整实盘 BUY(记账已落)
        rm0 = RiskManager()
        e0 = _live_engine(FakeRest(order_detail=_filled(), trades=[_trade()]), rm0)
        result = await e0.execute(_signal("BUY", 1.0, 100.0))
        assert result is not None and result["status"] == "FILLED"
        assert await _count(Position) == 1
        assert await _count(PositionLot) == 1

        # 重启: 交易所无挂单/成交(崩溃窗口已闭合)
        rest = _CrashRest(open_orders=[], trades=[], order_details={})
        rm, rec, engine = _restart(rest)
        await _reload_in_memory(rm, engine)

        unresolved = await rec.reconcile(SYMBOL)
        assert unresolved == []

        # 幂等: 内存态重建正确, 无重复记账(仍 1 仓 / 1 lot)
        assert rm.positions.get(SYMBOL).quantity == pytest.approx(1.0)
        assert await _count(Position) == 1
        assert await _count(PositionLot) == 1
        await assert_financial_invariants(engine, rm, 100.0, check_paper=False)


class TestCrashBeforeExchangeIdFreezes:
    async def test_crash_before_exchange_id_no_fabrication(self, db_tables):
        """下单前崩溃(UNKNOWN 无交易所 ID)且交易所查不到 -> 冻结, 绝不伪造成交。"""
        await _insert_order("crash-noeid", side="BUY", status="UNKNOWN", eid=None)

        rest = _CrashRest(open_orders=[], trades=[], order_details={})  # 查不到
        rm, rec, engine = _restart(rest)
        await _reload_in_memory(rm, engine)

        unresolved = await rec.reconcile(SYMBOL)
        assert any(u["type"] == "no_exchange_id"
                   and u["client_order_id"] == "crash-noeid" for u in unresolved)

        # 不伪造: 无持仓/镜像, 内存空仓
        assert await _count(Position) == 0
        assert await _count(PositionLot) == 0
        assert rm.positions.get(SYMBOL).quantity == pytest.approx(0.0)


class TestOrphanExchangeOrderFreezes:
    async def test_orphan_exchange_order_freezes(self, db_tables):
        """交易所存在本地无匹配的挂单(孤儿单) -> 冻结, 不自动改账。"""
        rest = _CrashRest(
            open_orders=[{"orderId": "777"}], trades=[], order_details={},
        )
        rm, rec, engine = _restart(rest)
        await _reload_in_memory(rm, engine)

        unresolved = await rec.reconcile(SYMBOL)
        assert any(u["type"] == "orphan_exchange_order"
                   and u["exchange_order_id"] == "777" for u in unresolved)
        assert await _count(Position) == 0
        assert await _count(PositionLot) == 0


class TestMultiCrashRestartCycles:
    async def test_multi_crash_windows_self_heal_invariants_hold(self, db_tables):
        """连续 5 个崩溃窗口各自自愈, 累积持仓单调正确, 每窗后不变量成立。"""
        price = 100.0
        for i in range(5):
            eid = str(900 + i)
            cid = f"crash-{i}"
            await _insert_order(cid, side="BUY", status="NEW", eid=eid)

            rest = _CrashRest(
                open_orders=[],
                trades=[{"orderId": eid, "id": 10 + i, "price": str(price), "qty": "1.0",
                         "quoteQty": str(price), "commission": "0",
                         "commissionAsset": "USDT", "time": 1700000000000}],
                order_details={eid: {"orderId": eid, "status": "FILLED",
                                     "executedQty": "1.0",
                                     "cummulativeQuoteQty": str(price)}},
            )
            rm, rec, engine = _restart(rest)
            await _reload_in_memory(rm, engine)

            assert await rec.reconcile(SYMBOL) == []
            assert rm.positions.get(SYMBOL).quantity == pytest.approx(float(i + 1))
            await assert_financial_invariants(engine, rm, price, check_paper=False)
            price += 1.0
