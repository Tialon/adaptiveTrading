"""V9.0 策略版本快照单元测试: StrategyVersionManager"""

import pytest


class TestStrategyVersionManager:
    async def test_snapshot_and_list(self, db_tables):
        from at50_strategy.strategy_version import StrategyVersionManager

        m = StrategyVersionManager()
        params = {"grid_upper_pct": 0.02, "portfolio_core_ratio": 0.40}
        vid = await m.snapshot("v1.0-test", note="测试", params=params)
        assert vid is not None

        rows = await m.list()
        assert len(rows) == 1
        assert rows[0]["version"] == "v1.0-test"
        assert rows[0]["params"]["portfolio_core_ratio"] == 0.40
        assert rows[0]["active"] is False

    async def test_snapshot_dedupes_same_version(self, db_tables):
        from at50_strategy.strategy_version import StrategyVersionManager

        m = StrategyVersionManager()
        a = await m.snapshot("v1.0-dup", params={"x": 1})
        b = await m.snapshot("v1.0-dup", params={"x": 2})
        assert a == b  # 同版本跳过, 返回已有 id
        rows = await m.list()
        assert len(rows) == 1

    async def test_activate(self, db_tables):
        from at50_strategy.strategy_version import StrategyVersionManager

        m = StrategyVersionManager()
        await m.snapshot("v-a", params={"a": 1})
        await m.snapshot("v-b", params={"b": 2})
        ok = await m.activate("v-a")
        assert ok is True
        rows = {r["version"]: r for r in await m.list()}
        assert rows["v-a"]["active"] is True
        assert rows["v-b"]["active"] is False

    async def test_activate_missing(self, db_tables):
        from at50_strategy.strategy_version import StrategyVersionManager

        m = StrategyVersionManager()
        assert await m.activate("nope") is False
