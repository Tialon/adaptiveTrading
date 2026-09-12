"""V9.0 M3.1: account_ledger 审计账本测试

验证: 每笔成交落 2 行(USDT + SOL)、before/after 守恒、related_order_id 正确、
失败降级不破坏主成交路径。
"""

import pytest
from sqlalchemy import select

from at01_common.models import AccountLedger
from at60_execution.execution_executor import ExecutionEngine
from at30_strategy.strategy_base import Signal, SignalSide
from at50_risk.risk_account_ledger import AccountLedgerWriter
from at50_risk.risk_manager import RiskManager


async def _ledger_rows() -> list[AccountLedger]:
    from at01_common.database import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        return list((await session.execute(select(AccountLedger))).scalars().all())


class TestAccountLedgerWriter:
    async def test_record_writes_two_rows(self, db_tables):
        w = AccountLedgerWriter()
        ok = await w.record(
            ts=1_700_000_000_000, symbol="BTCUSDT", bucket="trade", side="BUY",
            cash_before=10000.0, cash_after=9900.0,
            pos_before=0.0, pos_after=1.0,
            reason="test buy", related_order_id="at-abc",
        )
        assert ok is True

        rows = await _ledger_rows()
        assert len(rows) == 2
        assert {r.asset for r in rows} == {"USDT", "SOL"}

        usdt = next(r for r in rows if r.asset == "USDT")
        sol = next(r for r in rows if r.asset == "SOL")
        assert usdt.before_amount == 10000.0
        assert usdt.change_amount == -100.0
        assert usdt.after_amount == 9900.0
        assert sol.before_amount == 0.0
        assert sol.change_amount == 1.0
        assert sol.after_amount == 1.0
        # 两行共用同一 related_order_id
        assert usdt.related_order_id == "at-abc"
        assert sol.related_order_id == "at-abc"

    async def test_record_failure_degrades(self, db_tables, monkeypatch):
        """DB 会话异常 -> record 返回 False 而非抛出"""
        from at01_common import database as db_mod

        class _Boom:
            def __aenter__(self):
                raise RuntimeError("db down")

            def __aexit__(self, *a):
                return False

        monkeypatch.setattr(db_mod, "AsyncSessionLocal", lambda: _Boom())
        w = AccountLedgerWriter()
        ok = await w.record(
            ts=1, symbol="BTCUSDT", bucket="trade", side="BUY",
            cash_before=100.0, cash_after=90.0,
            pos_before=0.0, pos_after=1.0,
        )
        assert ok is False


class TestExecutionEngineLedger:
    async def test_paper_buy_sell_four_rows(self, db_tables):
        rm = RiskManager()
        rm.positions.positions.clear()
        engine = ExecutionEngine(risk_manager=rm)

        await engine.execute(Signal(
            symbol="BTCUSDT", strategy="trend", side=SignalSide.BUY,
            price=100.0, quantity=1.0, reason=["buy dip"],
        ))
        await engine.execute(Signal(
            symbol="BTCUSDT", strategy="trend", side=SignalSide.SELL,
            price=110.0, quantity=1.0, reason=["take profit"],
        ))

        rows = await _ledger_rows()
        assert len(rows) == 4  # 两笔成交 × (USDT + SOL)

        # 余额守恒: 每行 after == before + change
        for r in rows:
            assert r.after_amount == pytest.approx(r.before_amount + r.change_amount, abs=1e-6)

        # SOL 行: 先 +1 后 -1, 最终归零
        sol_rows = sorted([r for r in rows if r.asset == "SOL"], key=lambda r: r.ts)
        assert sol_rows[0].change_amount == pytest.approx(1.0)
        assert sol_rows[1].change_amount == pytest.approx(-1.0)

        # related_order_id 非空且成对(两笔成交)
        ids = {r.related_order_id for r in rows}
        assert len(ids) == 2
        assert all(i for i in ids)

        # 归因策略伞写入 reason
        assert any("trend" in r.reason for r in rows)

    async def test_ledger_failure_does_not_break_fill(self, db_tables, monkeypatch):
        rm = RiskManager()
        rm.positions.positions.clear()
        engine = ExecutionEngine(risk_manager=rm)

        async def _boom(**kwargs):
            return False

        monkeypatch.setattr(engine.account_ledger, "record", _boom)

        result = await engine.execute(Signal(
            symbol="BTCUSDT", strategy="trend", side=SignalSide.BUY,
            price=100.0, quantity=1.0,
        ))
        assert result is not None
        assert result["status"] == "FILLED"
        assert result["fill_qty"] == 1.0
