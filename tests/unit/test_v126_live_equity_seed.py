"""V12.6 P0: 实盘权益基线播种 —— 回归锚定

**修复的真实缺陷**: `settings.risk_initial_equity` 出厂默认 100000.0, 而唯一会覆盖它的
`mainnet_takeover` 只在主网跑(`not binance_testnet`)。于是 `live_testnet` 下本地权益恒为
100000, 与真实账户相差约 100%, 触发 `equity_drift`(单发即 KILLED) → SAFE_MODE →
无成交 → 漂移永不收敛 → **永久锁死**。Pi 实测 `reconcile_drift_pct=1.0` /
`reconcile_killed=81` / `orders_total=0`。

本文件锚定四件事:
1. 账户权益计算正确(纯函数);
2. 实盘启动播种后, 风控基线 = 交易所真实权益(而非配置默认);
3. 播种失败 / 权益为 0 → **拒绝启动**(fail-closed), 不用假基线顶替;
4. `equity_drift` 的真实 KILLED 判定**未被改软** —— 修的是基线, 不是检测。
"""

from __future__ import annotations

import pytest

from at01_common.live_equity import compute_account_equity


def _account(usdt_free: float, sol_free: float, usdt_locked: float = 0.0, sol_locked: float = 0.0):
    return {
        "balances": [
            {"asset": "USDT", "free": str(usdt_free), "locked": str(usdt_locked)},
            {"asset": "SOL", "free": str(sol_free), "locked": str(sol_locked)},
            {"asset": "BNB", "free": "1.0", "locked": "0"},  # 无关资产须被忽略
        ]
    }


class TestComputeAccountEquity:
    def test_equity_is_cash_plus_position_value(self):
        snap = compute_account_equity(_account(1000.0, 10.0), "SOLUSDT", 100.0)
        assert snap["cash"] == 1000.0
        assert snap["position"] == 10.0
        assert snap["equity"] == pytest.approx(2000.0)  # 1000 + 10*100

    def test_locked_balances_counted(self):
        """挂单锁定的资金仍是账户权益的一部分。"""
        snap = compute_account_equity(
            _account(100.0, 1.0, usdt_locked=50.0, sol_locked=0.5), "SOLUSDT", 100.0
        )
        assert snap["cash"] == 150.0
        assert snap["position"] == pytest.approx(1.5)
        assert snap["equity"] == pytest.approx(300.0)  # 150 + 1.5*100

    def test_empty_account_is_zero(self):
        snap = compute_account_equity({"balances": []}, "SOLUSDT", 100.0)
        assert snap["equity"] == 0.0


class TestSeedLiveEquityBaseline:
    """播种器: 只读交易所账户, 成功则覆盖 risk_initial_equity。"""

    @pytest.fixture
    def settings(self, monkeypatch):
        monkeypatch.setenv("PAPER_TRADING", "false")
        monkeypatch.setenv("BINANCE_TESTNET", "true")
        from at01_common.settings import get_settings

        get_settings.cache_clear()
        return get_settings()

    def _patch_client(self, monkeypatch, *, price=100.0, account=None, fail=None):
        """替换 BinanceRestClient 为假客户端。"""
        import at10_market.market_rest_client as mrc

        class FakeClient:
            def __init__(self, *a, **kw):
                pass

            async def connect(self):
                if fail == "connect":
                    raise ConnectionError("网络不可达")

            async def disconnect(self):
                pass

            async def get_price(self, symbol):
                if fail == "price":
                    raise RuntimeError("取价失败")
                return price

            async def get_account(self):
                if fail == "account":
                    raise RuntimeError("账户读取失败")
                return account if account is not None else _account(2800.0, 0.0)

        monkeypatch.setattr(mrc, "BinanceRestClient", FakeClient)

    async def test_seed_overwrites_default_baseline(self, monkeypatch, settings):
        """核心回归: 播种后基线 = 真实账户权益, 不再是 100000 默认值。"""
        from at01_common.live_equity import seed_live_equity_baseline

        assert settings.risk_initial_equity == 100000.0  # 出厂默认
        self._patch_client(monkeypatch, account=_account(2800.0, 2.0))

        result = await seed_live_equity_baseline(settings)

        assert result["ok"] is True
        assert result["original"] == 100000.0
        assert result["seeded"] == pytest.approx(3000.0)  # 2800 + 2*100
        assert settings.risk_initial_equity == pytest.approx(3000.0)

    async def test_seed_failure_is_fail_closed(self, monkeypatch, settings):
        """拿不到真实权益 → ok=False 且**基线保持原值**(调用方须据此拒绝启动)。"""
        from at01_common.live_equity import seed_live_equity_baseline

        self._patch_client(monkeypatch, fail="account")
        result = await seed_live_equity_baseline(settings)

        assert result["ok"] is False
        assert result["error"]
        assert settings.risk_initial_equity == 100000.0  # 未被污染

    async def test_connect_failure_is_fail_closed(self, monkeypatch, settings):
        from at01_common.live_equity import seed_live_equity_baseline

        self._patch_client(monkeypatch, fail="connect")
        result = await seed_live_equity_baseline(settings)

        assert result["ok"] is False
        assert settings.risk_initial_equity == 100000.0

    async def test_zero_equity_rejected(self, monkeypatch, settings):
        """权益为 0 → 拒绝(既无法建立基线, 也会让 sol_exposure_ratio 除零)。"""
        from at01_common.live_equity import seed_live_equity_baseline

        self._patch_client(monkeypatch, account={"balances": []})
        result = await seed_live_equity_baseline(settings)

        assert result["ok"] is False
        assert "权益为 0" in result["error"]
        assert settings.risk_initial_equity == 100000.0


class TestCurrentEquityFallback:
    """`current_equity` 不得再把「权益归零」falsy-回落成配置基线。"""

    def test_zero_equity_is_returned_not_masked(self):
        from at50_risk.risk_manager import RiskManager

        rm = RiskManager()
        rm.breaker.current_equity = 0.0  # 账户被清空的真实信号
        assert rm.current_equity == 0.0  # 旧实现会返回 risk_initial_equity(掩盖)

    def test_seeded_baseline_flows_into_limits(self):
        """播种后的基线必须传导到限额 —— 这是实盘仓位算对的前提。"""
        from at01_common.settings import get_settings
        from at50_risk.risk_manager import RiskManager

        settings = get_settings()
        settings.risk_initial_equity = 3000.0  # 模拟播种结果
        rm = RiskManager()
        rm.breaker.current_equity = 3000.0

        # 限额按真实权益的比例算, 而非按 100000
        assert rm.max_sol_exposure_quote == pytest.approx(3000.0 * settings.risk_max_sol_exposure)
        assert rm.current_equity == 3000.0


class TestDriftDetectionNotSoftened:
    """P0 修的是**基线**, 不是**检测** —— 真实漂移必须照常 KILL。"""

    def test_equity_drift_still_kills(self):
        from at60_execution.reconciliation_matrix import (
            ReconciliationMatrix,
            Severity,
        )

        m = ReconciliationMatrix()
        m.ingest(
            "equity",
            [{"type": "equity_drift", "symbol": "SOLUSDT", "local": 1000.0,
              "exchange": 500.0, "diff": 500.0}],
        )
        assert m.verdict().severity is Severity.KILLED

    def test_drift_within_tolerance_passes(self):
        """同源基线 + 小幅波动 → PASS(这才是修复后应有的状态)。"""
        from at60_execution.reconciliation import _split_asset  # noqa: F401  (存在性锚点)
        from at60_execution.reconciliation_matrix import ReconciliationMatrix, Severity

        m = ReconciliationMatrix()
        m.ingest("equity", [])  # 无差异
        assert m.verdict().severity is Severity.PASS
