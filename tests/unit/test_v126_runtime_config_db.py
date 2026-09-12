"""V12.6 P1: 运行参数入库 —— 回归锚定

锚定三条硬边界(见 `at01_common/runtime_config.py`):

1. **密钥永不入库** —— DB 会被备份/拷贝, 密钥入库等于扩散
2. **bootstrap 关键项排除** —— `DATABASE_URL` 等存进自己指向的库是循环依赖
3. **不热生效** —— 只在启动时叠加; 应用后**必须重跑 validate()**, 否则非法 DB 值绕过 fail-fast
"""

from __future__ import annotations

import pytest

from at01_common.runtime_config import (
    BOOTSTRAP_CRITICAL,
    apply_overrides,
    env_value_to_settings,
    override_allowlist,
    save_overrides,
    settings_value_to_env,
)

pytestmark = pytest.mark.usefixtures("db_tables")


class TestAllowlist:
    def test_secrets_never_enter_the_database(self):
        """含 KEY/SECRET/TOKEN/PASSWORD 的字段一律不进白名单 —— 这是本模块第一红线。"""
        allowed = override_allowlist()
        leaked = [k for k in allowed if any(t in k for t in ("KEY", "SECRET", "TOKEN", "PASSWORD"))]
        assert not leaked, f"密钥类字段不得入库: {leaked}"

    def test_bootstrap_critical_excluded(self):
        allowed = override_allowlist()
        assert not (set(allowed) & set(BOOTSTRAP_CRITICAL)), (
            "bootstrap 关键项必须排除 —— 它们要在数据库连上之前就有效"
        )

    def test_allowlist_is_nonempty_and_all_editable(self):
        allowed = override_allowlist()
        assert allowed, "白名单不应为空, 否则本功能没有意义"
        assert all(s.editable for s in allowed.values())


class TestSaveOverrides:
    async def test_rejects_secret_key(self):
        """提交密钥字段必须**被拒绝且不落库**, 不静默忽略。"""
        r = await save_overrides({"BINANCE_API_KEY": "oops"}, by="test")
        assert r["ok"] is False
        assert "BINANCE_API_KEY" in r["rejected"]
        assert await load_raw("BINANCE_API_KEY") is None

    async def test_rejects_bootstrap_critical(self):
        r = await save_overrides({"DATABASE_URL": "sqlite:///nope.db"}, by="test")
        assert r["ok"] is False
        assert "DATABASE_URL" in r["rejected"]

    async def test_writes_and_audits(self):
        """写入 + 落审计: 改风控阈值就是改交易行为, 必须可追溯。"""
        from at01_common.runtime_config import config_history

        r = await save_overrides(
            {"RISK_MAX_SINGLE_ORDER_PCT": 3}, by="operator", reason="降单笔上限"
        )
        assert r["ok"] is True
        assert await load_raw("RISK_MAX_SINGLE_ORDER_PCT") == "0.03"  # 库内存 env 域

        h = await config_history(limit=5)
        assert h and h[0]["key"] == "RISK_MAX_SINGLE_ORDER_PCT"
        assert h[0]["new_value"] == "0.03"
        assert h[0]["changed_by"] == "operator"
        assert h[0]["reason"] == "降单笔上限"

    async def test_same_value_does_not_spam_history(self):
        from at01_common.runtime_config import config_history

        await save_overrides({"RISK_MAX_SINGLE_ORDER_PCT": 3}, by="a")
        before = len(await config_history(limit=100))
        await save_overrides({"RISK_MAX_SINGLE_ORDER_PCT": 3}, by="b")
        assert len(await config_history(limit=100)) == before, "值没变不应产生审计噪声"


class TestApplyOverrides:
    async def test_db_beats_env(self):
        """优先级 DB > env: 写入后应用, settings 必须取 DB 的值。"""
        from at01_common.settings import get_settings

        await save_overrides({"RISK_MAX_SINGLE_ORDER_PCT": 3}, by="test")
        s = get_settings()
        assert s.risk_max_single_order_pct == pytest.approx(0.05)  # env/默认值

        report = await apply_overrides(s)

        assert report["count"] == 1
        assert s.risk_max_single_order_pct == pytest.approx(0.03)  # 3% 的 Settings 域值

    async def test_unavailable_db_degrades_to_env(self, monkeypatch):
        """DB 不可用 -> 优雅降级(返回空), 不得阻断启动。"""
        import at01_common.database as db_mod

        class Boom:
            def __call__(self, *a, **kw):
                raise RuntimeError("db down")

        monkeypatch.setattr(db_mod, "AsyncSessionLocal", Boom())
        from at01_common.settings import get_settings

        s = get_settings()
        report = await apply_overrides(s)
        assert report["count"] == 0
        assert s.risk_max_single_order_pct == pytest.approx(0.05)  # 仍是 env 值

    async def test_only_allowlisted_keys_are_applied(self):
        """历史残留(白名单收缩前写入的键)必须被跳过, 不得应用。"""
        from at01_common.database import AsyncSessionLocal
        from at01_common.models import RuntimeConfig
        from at01_common.settings import get_settings

        async with AsyncSessionLocal() as session:
            session.add(RuntimeConfig(key="NOT_A_REAL_KEY", value="x"))
            await session.commit()

        report = await apply_overrides(get_settings())
        assert "NOT_A_REAL_KEY" in report["skipped"]


class TestSaveDomainContract:
    """`save_overrides` 收 **UI 域**(页面自然域), 库内存 **env 域** —— 这条要钉死。

    设计时我自己就在测试里写了 UI 域的 "3" 却期望 env 域语义, 结果 `apply_overrides`
    读回来变成 3.0(比预期大 100 倍)。契约必须显式可测。
    """

    async def test_ui_domain_in_env_domain_stored(self):
        await save_overrides({"RISK_MAX_SINGLE_ORDER_PCT": 3})  # UI: 3%
        assert await load_raw("RISK_MAX_SINGLE_ORDER_PCT") == "0.03"  # env: 0.03

    async def test_round_trip_yields_settings_domain(self):
        from at01_common.settings import get_settings

        await save_overrides({"RISK_MAX_SINGLE_ORDER_PCT": 3})
        s = get_settings()
        await apply_overrides(s)
        assert s.risk_max_single_order_pct == pytest.approx(0.03)


class TestConversionRoundTrip:
    def test_pct_round_trip(self):
        """百分比在 UI 域是 3(%), env/Settings 域是 0.03 —— 换算必须可逆。"""
        spec = override_allowlist()["RISK_MAX_SINGLE_ORDER_PCT"]
        assert env_value_to_settings(spec, "0.03") == pytest.approx(0.03)

        from at01_common.settings import Settings

        s = Settings(risk_max_single_order_pct=0.03)
        assert settings_value_to_env(spec, s) == "0.03"

    def test_bool_round_trip(self):
        spec = override_allowlist()["DAILY_REPORT_ENABLED"]
        assert env_value_to_settings(spec, "false") is False
        assert env_value_to_settings(spec, "true") is True


async def load_raw(key: str) -> str | None:
    """直接读原始行, 用于断言「确实没落库」。"""
    from sqlalchemy import select

    from at01_common.database import AsyncSessionLocal
    from at01_common.models import RuntimeConfig

    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(select(RuntimeConfig).where(RuntimeConfig.key == key))
        ).scalars().first()
    return row.value if row else None
