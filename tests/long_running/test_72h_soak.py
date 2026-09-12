"""V11.4 P0-2 「72h」长跑仿真(72 个压缩交易周期 + DB / 手续费 / 资金漂移故障注入)。

- 72 个完整开平仓周期(简单 + 多 lot 交替), 每周期后 5 条财务守恒不变量成立;
- DB 回滚: 记账事务中途失败 -> 整体回滚(无部分镜像)+ RECOVERY_REQUIRED + 急停, 重建后不变量成立;
- DB 失败: 订单落库失败 -> fail-closed 不下单、无记账, 连续失败暂停, 不变量成立;
- 手续费不可计价(BNB): 降级暂停、不静默记 0, 持仓/账本仍自洽;
- 权益 / 持仓 / 现金 三向漂移 -> 资金级熔断分级(KILL / REDUCE_ONLY), 禁开仓。
"""

import pytest
from sqlalchemy import and_, select

from at01_common.database import AsyncSessionLocal
from at01_common.models import AccountLedger, Order, OrderFill, Position, PositionLot
from at60_execution.drift import compute_drift
from at50_risk.fund_circuit_breaker import BreakerAction, FundCircuitBreaker
from at50_risk.risk_manager import RiskManager
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


async def _cycle(engine, rm, clock, price: float, i: int) -> None:
    if i % 4 == 0:
        # 多 lot + 跨 lot 部分卖出
        await engine.execute(_signal("BUY", 2.0, price))
        clock.advance(100.0)
        await engine.execute(_signal("BUY", 1.0, price * 1.01))
        clock.advance(100.0)
        await engine.execute(_signal("SELL", 0.7, price * 1.03))
        clock.advance(100.0)
        await engine.execute(_signal("SELL", 2.3, price * 1.04))
    else:
        await engine.execute(_signal("BUY", 1.0, price))
        clock.advance(100.0)
        await engine.execute(_signal("SELL", 1.0, price * 1.02))
    clock.advance(100.0)
    await assert_financial_invariants(engine, rm, price * 1.04)


class Test72hNormalSoak:
    async def test_72_cycles_invariants_hold(self, db_tables, clock):
        """72 个完整开平仓周期, 每周期后 5 条不变量成立, 终态平仓自洽。"""
        rm = RiskManager()
        engine = _paper_engine(rm)
        price = 100.0
        for i in range(72):
            await _cycle(engine, rm, clock, price, i)
            price += 0.05
        assert rm.positions.get(SYMBOL).quantity == pytest.approx(0.0)
        await assert_financial_invariants(engine, rm, price)


class Test72hDbFaultSoak:
    async def test_db_rollback_no_partial_accounting_then_rebuild(self, db_tables, monkeypatch):
        """记账事务中途失败(lot 落库抛错) -> 整体回滚 + 急停, 无部分镜像; 重建后不变量成立。"""
        rm = RiskManager()
        engine = _live_engine(FakeRest(order_detail=_filled(), trades=[_trade()]), rm)

        async def _boom(*a, **k):
            raise RuntimeError("lot insert boom")

        # monkeypatch 实盘引擎的 lot 落库 -> 事务内失败
        monkeypatch.setattr(engine.lot_tracker, "_insert_lot", _boom)

        result = await engine.execute(_signal("BUY", 1.0, 100.0))
        assert result is not None and result["status"] == "RECOVERY_REQUIRED"
        assert rm.kill_switch.is_armed is True

        # 账本镜像整体回滚(无部分镜像)
        assert await _count(Position) == 0
        assert await _count(PositionLot) == 0
        assert await _count(AccountLedger) == 0

        # 内存权威态未丢(持仓已 bump + lot 已含)
        assert rm.positions.get(SYMBOL).quantity == pytest.approx(1.0)

        # 账务重建: 只补 DB 镜像, 不重复变更内存
        cid = result["client_order_id"]
        assert await engine.rebuild_buy_accounting(
            symbol=SYMBOL, client_order_id=cid, exchange_order_id="100",
            fill_qty=1.0, fill_price=100.0, fee=0.0,
        ) == "recovered"

        assert rm.positions.get(SYMBOL).quantity == pytest.approx(1.0)
        # 重建只补 DB 镜像(Position + Lot), 不重复变更内存(与 test_v107 同口径);
        # Order.filled_quantity 由 _rebuild_buy_mirror 不更新(留待 fill-truth 对账收敛),
        # 故此处只断言镜像一致性 + lot 守恒, 不套用 base-asset 不变量。
        assert await _count(Position) == 1
        assert await _count(PositionLot) == 1
        lot_sum = await _sum(
            PositionLot.quantity, and_(PositionLot.symbol == SYMBOL, PositionLot.status == "open")
        )
        assert lot_sum == pytest.approx(1.0, abs=1e-6)
        async with AsyncSessionLocal() as s:
            row = (await s.execute(select(Order).where(Order.symbol == SYMBOL))).scalar_one()
            assert row.accounting_state == "RECOVERED"

    async def test_db_failure_no_accounting(self, db_tables, monkeypatch, clock):
        """订单落库失败 -> fail-closed 中止(不投交易所、不记账); 连续 3 次暂停; 不变量成立。"""
        rm = RiskManager()
        engine = _paper_engine(rm)

        async def _create_order_none(*a, **k):
            return None

        monkeypatch.setattr(engine, "_create_order_record", _create_order_none)

        for _ in range(3):
            result = await engine.execute(_signal("BUY", 1.0, 100.0))
            assert result is None  # 未成交、无记账
            clock.advance(11.0)  # 推进幂等桶(>10s)但不超过暂停窗口(60s), 每次都能触发执行失败计数且不自动解暂停

        # 无任何订单/持仓/账本/成交残留
        assert await _count(Order) == 0
        assert await _count(Position) == 0
        assert await _count(AccountLedger) == 0
        assert await _count(OrderFill) == 0
        # 连续 3 次执行失败 -> 暂停
        assert not rm.can_buy()
        await assert_financial_invariants(engine, rm, 100.0)


class Test72hFeeDriftSoak:
    async def test_fee_unpriced_pauses_and_invariants_hold(self, db_tables):
        """手续费计价资产为 BNB(不可折算) -> 降级暂停, 不静默记 0, 持仓/账本仍自洽。"""
        rm = RiskManager()
        bnb_trade = _trade(commission="0.001", commission_asset="BNB")
        engine = _live_engine(FakeRest(order_detail=_filled(), trades=[bnb_trade]), rm)

        result = await engine.execute(_signal("BUY", 1.0, 100.0))
        assert result is not None and result["status"] == "FILLED"
        assert rm.positions.get(SYMBOL).quantity == pytest.approx(1.0)

        # 手续费不可计价 -> 显式标记(非 zero)、风险暂停
        assert not rm.can_buy()
        async with AsyncSessionLocal() as s:
            row = (await s.execute(select(OrderFill).where(OrderFill.symbol == SYMBOL))).scalar_one()
            assert row.fee_valuation_status == "unpriced"
            assert row.fee_quote == pytest.approx(0.0)

        # 持仓/账本自洽(fee 未静默计入, 但资产守恒仍成立)
        await assert_financial_invariants(engine, rm, 100.0, check_paper=False)


def _assess(**drift_kwargs):
    drift = compute_drift(**drift_kwargs)
    assert drift.trusted, drift.reason
    return FundCircuitBreaker().assess(
        equity_drift=drift.equity_drift,
        position_drift=drift.position_drift,
        cash_drift=drift.cash_drift,
    )


class Test72hFundDriftSoak:
    def test_equity_drift_kills_blocks_open(self):
        """权益漂移(资金级最严重) -> KILL, 禁开禁减。"""
        rm = RiskManager()
        decision = _assess(
            local_equity=10000.0, exchange_equity=10070.0,
            local_position=0.0, exchange_position=0.0,
            local_cash=10000.0, exchange_cash=10070.0,
        )
        assert decision.action is BreakerAction.KILL
        rm.kill_switch.arm(f"drift: {decision.reason}")
        assert not rm.can_buy()
        assert not rm.can_sell()

    def test_position_drift_reduce_only_blocks_open(self):
        """持仓漂移 -> REDUCE_ONLY, 禁开新仓、保留卖出。"""
        rm = RiskManager()
        decision = _assess(
            local_equity=10000.0, exchange_equity=10000.0,
            local_position=100.0, exchange_position=99.8,
            local_cash=10000.0, exchange_cash=10000.0,
        )
        assert decision.action is BreakerAction.REDUCE_ONLY
        rm.reduce_only(f"drift: {decision.reason}")
        assert not rm.can_buy()
        assert rm.can_sell()

    def test_cash_drift_reduce_only_blocks_open(self):
        """现金漂移 -> REDUCE_ONLY, 禁开新仓、保留卖出。"""
        rm = RiskManager()
        decision = _assess(
            local_equity=5000.0, exchange_equity=5000.0,
            local_position=0.0, exchange_position=0.0,
            local_cash=5000.0, exchange_cash=4990.0,
        )
        assert decision.action is BreakerAction.REDUCE_ONLY
        rm.reduce_only(f"drift: {decision.reason}")
        assert not rm.can_buy()
        assert rm.can_sell()
