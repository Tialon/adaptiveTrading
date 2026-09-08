"""V11.4 P0-2 「24h」长跑仿真(24 个压缩交易周期 + 交易链路故障注入)。

不真等 24 小时: 用可拨动逻辑时钟 + 合成行情驱动纸面引擎跑 24 个完整开平仓周期,
每周期后断言 5 条财务守恒不变量。随后对交易链路故障(WS 断开 / REST 超时 / 重复成交 /
部分成交 / 部分成交后撤单 / 未知订单)逐一注入, 断言「故障不漂移、可恢复、不变量持续成立」。

全部 deterministic: 无真实 Binance、无真实等待、无随机价格。
"""

import asyncio

import pytest

from at01_common.database import AsyncSessionLocal
from at01_common.models import Order, OrderFill, PositionLot
from at20_market.market_rest_client import BinanceAPIError
from at50_execution.order_recovery import OrderRecoveryEngine
from at60_risk.risk_manager import RiskManager
from tests.long_running._harness import (
    FakeRest,
    _count,
    _filled,
    _live_engine,
    _paper_engine,
    _signal,
    _sum,
    _trade,
    assert_financial_invariants,
)

SYMBOL = "SOLUSDT"


async def _simple_cycle(engine, rm, clock, price: float) -> None:
    """一次完整开平仓: BUY 1.0 @ price -> SELL 1.0 @ price*1.02。"""
    r = await engine.execute(_signal("BUY", 1.0, price))
    assert r is not None and r["status"] == "FILLED", r
    clock.advance(100.0)
    r = await engine.execute(_signal("SELL", 1.0, price * 1.02))
    assert r is not None and r["status"] == "FILLED", r
    clock.advance(100.0)
    await assert_financial_invariants(engine, rm, price * 1.02)


async def _multi_lot_cycle(engine, rm, clock, price: float) -> None:
    """多 lot + 跨 lot 部分卖出: 覆盖 FIFO 分配与多批次成本。"""
    await engine.execute(_signal("BUY", 2.0, price))
    clock.advance(100.0)
    await engine.execute(_signal("BUY", 1.0, price * 1.01))
    clock.advance(100.0)
    await engine.execute(_signal("SELL", 0.7, price * 1.03))
    clock.advance(100.0)
    await engine.execute(_signal("SELL", 2.3, price * 1.04))
    clock.advance(100.0)
    await assert_financial_invariants(engine, rm, price * 1.04)


class Test24hNormalSoak:
    async def test_24_cycles_invariants_hold(self, db_tables, clock):
        """24 个完整开平仓周期(简单 + 多 lot 交替), 每周期后 5 条不变量成立。"""
        rm = RiskManager()
        engine = _paper_engine(rm)
        price = 100.0
        for i in range(24):
            if i % 3 == 0:
                await _multi_lot_cycle(engine, rm, clock, price)
            else:
                await _simple_cycle(engine, rm, clock, price)
            price += 0.1

        # 全部周期结束: 平仓(0 持仓)、账本/成交/lot 全链自洽
        assert rm.positions.get(SYMBOL).quantity == pytest.approx(0.0)
        await assert_financial_invariants(engine, rm, price)


class Test24hFaultSoak:
    async def test_ws_disconnect_blocks_then_recovers_invariants_hold(self, db_tables, clock):
        """WS 断开(行情静默) -> 暂停禁开; 窗口到期恢复 -> 继续交易, 不变量持续成立。"""
        rm = RiskManager()
        engine = _paper_engine(rm)
        await _simple_cycle(engine, rm, clock, 100.0)

        # 模拟 WS 断开 -> 行情静默 -> 暂停
        rm.pause("行情静默(ws disconnect)")
        assert not rm.can_buy()
        # 暂停期间不变量仍成立(无交易发生)
        await assert_financial_invariants(engine, rm, 102.0)

        # 逻辑时间推进超过暂停窗口(60s)后自动恢复
        clock.advance(61.0)
        assert rm.can_buy()
        await _simple_cycle(engine, rm, clock, 103.0)

    async def test_rest_timeout_unknown_then_recovers(self, db_tables, clock):
        """REST 下单超时 -> UNKNOWN(不静默记账) -> 交易所真相收敛, 不变量成立。"""
        rm = RiskManager()
        engine = _live_engine(FakeRest(timeout=True), rm)
        result = await engine.execute(_signal("BUY", 1.0, 100.0))
        assert result is not None and result["status"] == "UNKNOWN"
        assert result["fill_qty"] == 0.0
        assert rm.positions.get(SYMBOL).quantity == 0.0
        assert await _count(OrderFill) == 0  # 未静默记账

        # 交易所真相: 实际已成交 -> 恢复收敛
        cid = result["client_order_id"]
        engine.rest = FakeRest(order_detail=_filled(), trades=[_trade()])
        recovery = OrderRecoveryEngine(engine.rest, engine, rm)
        assert await recovery.recover(SYMBOL) == []
        assert rm.positions.get(SYMBOL).quantity == pytest.approx(1.0)
        await assert_financial_invariants(engine, rm, 100.0, check_paper=False)

    async def test_duplicate_fill_idempotent_invariants_hold(self, db_tables):
        """重复成交投递 -> OrderFill 幂等去重、不重复记账, 不变量成立。"""
        rm = RiskManager()
        engine = _live_engine(FakeRest(order_detail=_filled(), trades=[_trade(), dict(_trade())]), rm)
        result = await engine.execute(_signal("BUY", 1.0, 100.0))
        assert result is not None and result["status"] == "FILLED"
        assert await _count(OrderFill) == 1  # 幂等去重
        assert rm.positions.get(SYMBOL).quantity == pytest.approx(1.0)
        await assert_financial_invariants(engine, rm, 100.0, check_paper=False)

    async def test_partial_fill_accounts_partial_invariants_hold(self, db_tables, monkeypatch):
        """部分成交 -> 只记已成交部分(PARTIALLY_FILLED), 不变量成立。"""
        async def _noop_sleep(*a, **k):
            return None

        monkeypatch.setattr(asyncio, "sleep", _noop_sleep)
        partial = {"orderId": "100", "status": "PARTIALLY_FILLED",
                   "executedQty": "0.5", "cummulativeQuoteQty": "50.0"}
        canceled = {"orderId": "100", "status": "CANCELED",
                    "executedQty": "0.5", "cummulativeQuoteQty": "50.0"}
        half = _trade(qty="0.5", quote="50.0")
        rm = RiskManager()
        engine = _live_engine(FakeRest(order_queue=[partial, canceled], trades=[half]), rm)
        result = await engine.execute(_signal("BUY", 1.0, 100.0))
        assert result is not None and result["status"] == "PARTIALLY_FILLED"
        assert result["fill_qty"] == pytest.approx(0.5)
        assert rm.positions.get(SYMBOL).quantity == pytest.approx(0.5)
        await assert_financial_invariants(engine, rm, 100.0, check_paper=False)

    async def test_cancel_after_partial_fill_invariants_hold(self, db_tables, monkeypatch):
        """部分成交后撤单 -> 保留已成交部分、不回吐, 不变量成立。"""
        async def _noop_sleep(*a, **k):
            return None

        monkeypatch.setattr(asyncio, "sleep", _noop_sleep)
        partial = {"orderId": "100", "status": "PARTIALLY_FILLED",
                   "executedQty": "0.3", "cummulativeQuoteQty": "30.0"}
        canceled = {"orderId": "100", "status": "CANCELED",
                    "executedQty": "0.3", "cummulativeQuoteQty": "30.0"}
        rm = RiskManager()
        engine = _live_engine(
            FakeRest(order_queue=[partial, canceled], trades=[_trade(qty="0.3", quote="30.0")]), rm
        )
        result = await engine.execute(_signal("BUY", 1.0, 100.0))
        assert result is not None and result["status"] == "PARTIALLY_FILLED"
        assert result["fill_qty"] == pytest.approx(0.3)
        assert rm.positions.get(SYMBOL).quantity == pytest.approx(0.3)
        await assert_financial_invariants(engine, rm, 100.0, check_paper=False)

    async def test_unknown_order_blocks_accounting_then_recovers(self, db_tables):
        """5xx 结果未明 -> UNKNOWN(不静默记账) -> 恢复收敛, 不变量成立。"""
        rm = RiskManager()
        engine = _live_engine(FakeRest(five_xx=True), rm)
        result = await engine.execute(_signal("BUY", 1.0, 100.0))
        assert result is not None and result["status"] == "UNKNOWN"
        assert rm.positions.get(SYMBOL).quantity == 0.0
        assert await _count(OrderFill) == 0

        cid = result["client_order_id"]
        engine.rest = FakeRest(order_detail=_filled(), trades=[_trade()])
        recovery = OrderRecoveryEngine(engine.rest, engine, rm)
        assert await recovery.recover(SYMBOL) == []
        assert rm.positions.get(SYMBOL).quantity == pytest.approx(1.0)
        await assert_financial_invariants(engine, rm, 100.0, check_paper=False)
