"""V9.0 M3.2: Regime 条件滑点测试

验证: regime 命中放大 bps、未知 regime 回退平铺、execute_at_open 透传 regime。
"""

import pytest

from at70_backtest.backtest_execution import NextBarExecutor, SlippageModel


class TestSlippageModel:
    def test_regime_hit_amplifies(self):
        m = SlippageModel(10.0, {"PANIC": 50.0})
        # PANIC 下 50bps
        assert m.buy_price(100.0, "PANIC") == pytest.approx(100.0 * (1 + 0.005))
        assert m.sell_price(100.0, "PANIC") == pytest.approx(100.0 * (1 - 0.005))

    def test_unknown_regime_falls_back_flat(self):
        m = SlippageModel(10.0, {"PANIC": 50.0})
        assert m.buy_price(100.0, "BULL") == pytest.approx(100.0 * (1 + 0.001))
        assert m.sell_price(100.0, "NORMAL") == pytest.approx(100.0 * (1 - 0.001))

    def test_none_regime_falls_back_flat(self):
        m = SlippageModel(10.0, {"PANIC": 50.0})
        assert m.buy_price(100.0) == pytest.approx(100.0 * (1 + 0.001))
        assert m.sell_price(100.0) == pytest.approx(100.0 * (1 - 0.001))

    def test_case_insensitive_regime_key(self):
        m = SlippageModel(10.0, {"PANIC": 50.0})
        assert m.buy_price(100.0, "panic") == pytest.approx(100.0 * (1 + 0.005))


class TestNextBarExecutor:
    def test_execute_at_open_passes_regime(self):
        ex = NextBarExecutor()
        ex.submit({"side": "BUY", "qty": 1.0, "regime": "PANIC"})
        ex.submit({"side": "SELL", "qty": 1.0})  # 无 regime

        slip = SlippageModel(10.0, {"PANIC": 50.0})
        ready = ex.execute_at_open(100.0, slip)
        buy = ready[0]
        sell = ready[1]
        assert buy["exec_price"] == pytest.approx(100.0 * (1 + 0.005))  # PANIC 50bps
        assert sell["exec_price"] == pytest.approx(100.0 * (1 - 0.001))  # 回退平铺 10bps


class TestSettingsParsing:
    def test_slippage_regime_bps_map(self):
        from at01_common.settings import get_settings

        m = get_settings().slippage_regime_bps_map
        assert m["PANIC"] == 50.0
        assert m["VOLATILE"] == 20.0
        assert m["BEAR"] == 15.0
