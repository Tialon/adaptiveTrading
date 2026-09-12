"""V12.7 运行模式解析(ModeResolver)—— 回归锚定

对外只有三个模式: 模拟(paper) / 测试网(testnet) / 实盘(live)。
本文件锚定四件事:

1. **解析正确** —— 三个模式各自推导出正确的内部字段;
2. **旧配置兼容**(§17) —— 未设 `TRADING_MODE` 时按旧配置推导, 老 `.env` 照常可用;
3. **冲突 fail-closed**(§18) —— 新旧配置明显矛盾时**不静默选择其一**, 直接拒绝启动;
4. **实盘确认是人工动作**(§11/§12) —— 解析器**不代填** `LIVE_TRADING_CONFIRM` /
   `MAINNET_API_SCOPE_CONFIRMED`, 否则"刻意确认"会退化成"配置副作用"。
"""

from __future__ import annotations

import pytest

from at01_common.trading_mode import (
    MarketDataSource,
    TradingMode,
    resolve_mode,
)


def _r(**kw):
    base = dict(
        trading_mode="", paper_trading=True, binance_testnet=True,
        run_testnet_trading="", live_trading_confirm="",
        mainnet_api_scope_confirmed=False, explicitly_set=frozenset(),
    )
    base.update(kw)
    return resolve_mode(**base)


class TestExplicitMode:
    """`TRADING_MODE` 一设即为权威。"""

    def test_paper(self):
        r = _r(trading_mode="paper")
        assert r.ok and r.mode is TradingMode.PAPER
        assert r.derived["paper_trading"] is True
        assert r.source == "TRADING_MODE"

    def test_testnet(self):
        r = _r(trading_mode="testnet")
        assert r.ok and r.mode is TradingMode.TESTNET
        assert r.derived["paper_trading"] is False
        assert r.derived["binance_testnet"] is True
        assert r.derived["run_testnet_trading"] == "1"

    def test_live_with_confirms(self):
        r = _r(trading_mode="live", live_trading_confirm="true",
               mainnet_api_scope_confirmed=True)
        assert r.ok and r.mode is TradingMode.LIVE
        assert r.derived["paper_trading"] is False
        assert r.derived["binance_testnet"] is False
        assert r.market_data_source is MarketDataSource.MAINNET

    def test_case_and_whitespace_tolerated(self):
        assert _r(trading_mode="  PAPER ").mode is TradingMode.PAPER

    @pytest.mark.parametrize("bad", ["simulation", "real", "mainnet", "0", "yes"])
    def test_invalid_value_rejected(self, bad):
        r = _r(trading_mode=bad)
        assert not r.ok
        assert "非法" in r.error


class TestLiveRequiresHumanConfirmation:
    """§11/§12: 实盘确认必须是**人工动作**, 解析器不代填。"""

    def test_live_without_confirms_fails_closed(self):
        r = _r(trading_mode="live")
        assert not r.ok
        assert "LIVE_TRADING_CONFIRM" in r.error
        assert "MAINNET_API_SCOPE_CONFIRMED" in r.error
        # 关键: 不得把确认字段塞进 derived
        assert "live_trading_confirm" not in r.derived
        assert "mainnet_api_scope_confirmed" not in r.derived

    def test_live_with_only_one_confirm_fails(self):
        r = _r(trading_mode="live", live_trading_confirm="true")
        assert not r.ok
        assert "MAINNET_API_SCOPE_CONFIRMED" in r.error


class TestLegacyCompatibility:
    """§17: 未设 `TRADING_MODE` 时按旧配置推导 —— 老 `.env` 必须照常可用。"""

    def test_legacy_paper_default(self):
        r = _r(paper_trading=True, binance_testnet=True)
        assert r.ok and r.mode is TradingMode.PAPER
        assert r.source == "legacy"
        assert r.derived == {}, "旧配置已是权威, 不该回写"
        assert r.legacy_hint, "应提示可以改用 TRADING_MODE"

    def test_legacy_testnet(self):
        r = _r(paper_trading=False, binance_testnet=True, run_testnet_trading="1")
        assert r.ok and r.mode is TradingMode.TESTNET

    def test_legacy_live(self):
        r = _r(paper_trading=False, binance_testnet=False, live_trading_confirm="true")
        assert r.ok and r.mode is TradingMode.LIVE

    def test_undetermined_testnet_fails_closed(self):
        """paper=false + testnet=true 但没 RUN_TESTNET_TRADING → 不猜。"""
        r = _r(paper_trading=False, binance_testnet=True, run_testnet_trading="")
        assert not r.ok
        assert "RUN_TESTNET_TRADING" in r.error

    def test_undetermined_mainnet_fails_closed(self):
        r = _r(paper_trading=False, binance_testnet=False, live_trading_confirm="")
        assert not r.ok
        assert "LIVE_TRADING_CONFIRM" in r.error


class TestConflict:
    """§18: 新旧配置矛盾 → 报错, 不静默选择其一。"""

    def test_live_vs_paper_conflict(self):
        r = _r(trading_mode="live", live_trading_confirm="true",
               mainnet_api_scope_confirmed=True,
               paper_trading=True, explicitly_set={"paper_trading"})
        assert not r.ok
        assert "冲突" in r.error
        assert "paper_trading" in r.error

    def test_testnet_vs_testnet_flag_conflict(self):
        """TRADING_MODE=testnet 但显式写了 BINANCE_TESTNET=false → 冲突。"""
        r = _r(trading_mode="testnet", binance_testnet=False,
               explicitly_set={"binance_testnet"})
        assert not r.ok
        assert "binance_testnet" in r.error

    def test_blank_explicit_value_is_not_a_conflict(self):
        """显式留空 ≠ 表态为空。`.env` 里留着 `RUN_TESTNET_TRADING=` 不该挡住 testnet。"""
        r = _r(trading_mode="testnet", run_testnet_trading="",
               explicitly_set={"run_testnet_trading"})
        assert r.ok and r.mode is TradingMode.TESTNET

    def test_agreeing_explicit_values_are_fine(self):
        r = _r(trading_mode="testnet", binance_testnet=True,
               explicitly_set={"binance_testnet"})
        assert r.ok


class TestMarketDataSourceIsOrthogonal:
    """§7: 行情数据源与模式正交 —— 「模拟 + 主网行情」是高级选项, 不是第四种模式。"""

    def test_paper_on_mainnet_data(self):
        r = _r(trading_mode="paper", binance_testnet=False,
               explicitly_set={"binance_testnet"})
        assert r.ok
        assert r.mode is TradingMode.PAPER
        assert r.market_data_source is MarketDataSource.MAINNET

    def test_paper_on_testnet_data(self):
        r = _r(trading_mode="paper", binance_testnet=True)
        assert r.mode is TradingMode.PAPER
        assert r.market_data_source is MarketDataSource.TESTNET

    def test_live_always_mainnet(self):
        r = _r(trading_mode="live", live_trading_confirm="true",
               mainnet_api_scope_confirmed=True)
        assert r.market_data_source is MarketDataSource.MAINNET

    def test_only_three_modes_exist(self):
        """对外只有三个模式 —— paper_mainnet 之类不得成为第四个 TradingMode。"""
        assert {m.value for m in TradingMode} == {"paper", "testnet", "live"}


class TestSafetyInvariantsUnchanged:
    """§12/§13: 模式简化**不得**改变既有安全判定。"""

    def test_paper_mode_never_yields_order_capability(self):
        """模拟模式推导结果必须让 ExecutionEngine 走纸面分支(paper_trading=True)。"""
        r = _r(trading_mode="paper", binance_testnet=False,
               explicitly_set={"binance_testnet"})
        assert r.derived["paper_trading"] is True

    def test_testnet_never_points_at_mainnet(self):
        r = _r(trading_mode="testnet")
        assert r.derived["binance_testnet"] is True
        assert r.market_data_source is MarketDataSource.TESTNET

    def test_live_still_requires_both_confirmations(self):
        """解析器不给确认, 因此主网守卫 `mainnet_blocked_reason()` 仍然拦得住。"""
        from at01_common.settings import Settings

        s = Settings(_env_file=None, paper_trading=False, binance_testnet=False,
                     live_trading_confirm="", mainnet_api_scope_confirmed=False)
        assert s.mainnet_blocked_reason(), "未确认时必须拦"

    def test_resolver_output_feeds_guards_consistently(self):
        """解析结果写回 settings 后, 守卫读到的必须是自洽的值。"""
        from at01_common.settings import Settings

        s = Settings(_env_file=None, trading_mode="testnet")
        r = resolve_mode(
            trading_mode="testnet", paper_trading=s.paper_trading,
            binance_testnet=s.binance_testnet,
            run_testnet_trading=s.run_testnet_trading,
            live_trading_confirm=s.live_trading_confirm,
            mainnet_api_scope_confirmed=s.mainnet_api_scope_confirmed,
            # 用固定的集合隔离: 测试进程的 os.environ 里带着 conftest 设的
            # PAPER_TRADING=true, 那会让"冲突判定"正确地把本用例拦下 —— 不是我们要测的路径。
            explicitly_set=frozenset(),
        )
        for k, v in r.derived.items():
            setattr(s, k, v)

        # 写回后: 不是纸面、指向测试网、主网守卫不适用
        assert s.paper_trading is False
        assert s.binance_testnet is True
        assert s.mainnet_blocked_reason() is None
        # 测试网闸门应放行(前提齐备)
        from at01_common.testnet_gate import testnet_preflight

        p = testnet_preflight(binance_testnet=True, paper_trading=False, live_trading=False,
                              run_testnet_trading=s.run_testnet_trading,
                              credentials_present=True, git_sha="x", symbol="SOLUSDT")
        assert p["allowed"] is True
