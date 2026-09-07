"""V9.0 成交日志单元测试: TradingJournal(成交闭环记录)"""

import pytest


class TestTradingJournal:
    async def test_record_and_read(self, db_tables):
        from at40_journal.trading_journal import TradingJournal

        j = TradingJournal()
        jid = await j.record({
            "symbol": "SOLUSDT", "strategy": "decision", "bucket": "trade",
            "entry_ts": 1_700_000_000.0, "entry_price": 100.0,
            "exit_price": 120.0, "quantity": 5.0, "realized_pnl": 100.0,
            "peak_price": 125.0, "trough_price": 95.0,
        })
        assert jid is not None

        rows = await j.recent("SOLUSDT")
        assert len(rows) == 1
        r = rows[0]
        assert r["realized_pnl"] == pytest.approx(100.0)
        assert r["strategy"] == "decision"
        # 最大浮盈 = (peak - entry) * qty = 25 * 5
        assert r["max_profit"] == pytest.approx(125.0)
        # 最大回撤 = (entry - trough) * qty = 5 * 5
        assert r["max_drawdown"] == pytest.approx(25.0)

    async def test_no_entry_ts_zero_holding(self, db_tables):
        from at40_journal.trading_journal import TradingJournal

        j = TradingJournal()
        await j.record({
            "symbol": "SOLUSDT", "strategy": "exit", "bucket": "trade",
            "entry_ts": 0.0, "entry_price": 100.0,
            "exit_price": 110.0, "quantity": 1.0, "realized_pnl": 10.0,
            "peak_price": 110.0, "trough_price": 100.0,
        })
        rows = await j.recent("SOLUSDT")
        assert rows[0]["holding_seconds"] == 0.0

    async def test_empty_recent(self, db_tables):
        from at40_journal.trading_journal import TradingJournal

        j = TradingJournal()
        assert await j.recent("SOLUSDT") == []
