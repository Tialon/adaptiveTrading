"""V12 §24-25: HODL 基准(接管基线冻结 + 三策略权益/Alpha 计算)

- §24: 接管时刻记录初始权益 / 初始 SOL 数量 / 初始 SOL 价格, 只记一次、冻结不可覆盖。
- §25: 每日计算 Adaptive(实际)/ HODL(买入持有)/ Cash(全现金)三策略权益, Alpha = Adaptive - HODL。
"""

import pytest

from at40_journal.hodl_benchmark import HodlBenchmark, compute_benchmark


def _baseline(equity=10000.0, sol_qty=50.0, sol_price=100.0):
    return {"initial_equity": equity, "initial_sol_qty": sol_qty, "initial_sol_price": sol_price}


class TestComputeBenchmark:
    """纯函数计算(不落库)。"""

    def test_hodl_tracks_price_only(self):
        # equity0=10000, Q0=50, P0=100 -> U0=5000 现金。
        # P=120 -> hodl = 5000 + 50*120 = 11000(只随价格波动, 与策略无关)。
        r = compute_benchmark(_baseline(), current_equity=10800.0, current_price=120.0)
        assert r.hodl_equity == pytest.approx(11000.0)
        assert r.cash_equity == pytest.approx(10000.0)
        assert r.adaptive_equity == pytest.approx(10800.0)
        assert r.alpha == pytest.approx(10800.0 - 11000.0)  # -200, 跑输持有
        assert r.hodl_pnl == pytest.approx(1000.0)
        assert r.adaptive_pnl == pytest.approx(800.0)

    def test_alpha_positive_when_beat_hold(self):
        r = compute_benchmark(_baseline(), current_equity=11500.0, current_price=100.0)
        assert r.alpha > 0

    def test_invalid_baseline_returns_none(self):
        assert compute_benchmark(_baseline(equity=0.0), 100.0, 100.0) is None
        assert compute_benchmark(_baseline(sol_qty=-1.0), 100.0, 100.0) is None

    def test_pure_cash_initial_holds_flat(self):
        # 全部现金接管(Q0=0): hodl == cash == equity0, Alpha 恒 0。
        r = compute_benchmark(_baseline(sol_qty=0.0), current_equity=10000.0, current_price=200.0)
        assert r.hodl_equity == pytest.approx(10000.0)
        assert r.alpha == pytest.approx(0.0)

    def test_to_dict_shape(self):
        r = compute_benchmark(_baseline(), current_equity=10800.0, current_price=120.0)
        d = r.to_dict()
        for key in ("adaptive_equity", "hodl_equity", "cash_equity", "alpha",
                    "adaptive_pnl", "hodl_pnl", "initial_equity"):
            assert key in d


class TestHodlBenchmarkPersistence:
    async def test_record_then_load(self, db_tables):
        b = HodlBenchmark("SOLUSDT")
        assert await b.record_baseline(10000.0, 50.0, 100.0) is True
        b2 = HodlBenchmark("SOLUSDT")  # 新实例, 强制从 DB 读(非缓存)
        bl = await b2.load_baseline()
        assert bl["initial_equity"] == pytest.approx(10000.0)
        assert bl["initial_sol_qty"] == pytest.approx(50.0)
        assert bl["initial_sol_price"] == pytest.approx(100.0)
        assert await b2.has_baseline() is True

    async def test_frozen_no_overwrite(self, db_tables):
        b = HodlBenchmark("SOLUSDT")
        assert await b.record_baseline(10000.0, 50.0, 100.0) is True
        # 二次记录(重复接管)必须拒绝覆盖, 保持原始基线冻结。
        assert await b.record_baseline(20000.0, 999.0, 150.0) is False
        bl = await b.load_baseline()
        assert bl["initial_equity"] == pytest.approx(10000.0)
        assert bl["initial_sol_qty"] == pytest.approx(50.0)

    async def test_invalid_record_rejected(self, db_tables):
        b = HodlBenchmark("SOLUSDT")
        assert await b.record_baseline(0.0, 50.0, 100.0) is False
        assert await b.has_baseline() is False

    async def test_no_baseline_evaluate_none(self, db_tables):
        b = HodlBenchmark("SOLUSDT")
        assert await b.has_baseline() is False
        assert await b.evaluate(10000.0, 100.0) is None

    async def test_evaluate_roundtrip(self, db_tables):
        b = HodlBenchmark("SOLUSDT")
        await b.record_baseline(10000.0, 50.0, 100.0)  # U0=5000
        r = await b.evaluate(11000.0, 120.0)  # hodl = 5000 + 50*120 = 11000
        assert r is not None
        assert r.hodl_equity == pytest.approx(11000.0)
        assert r.alpha == pytest.approx(0.0)
