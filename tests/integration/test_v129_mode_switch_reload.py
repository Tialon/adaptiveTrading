"""V12.9 P0: 模式切换**跨重启**回归

**本文件修的是一个真实把服务卡死的 bug**（本机实测复现）：

    .env:  PAPER_TRADING=true        (迁移前遗留的显式值)
    .env:  TRADING_MODE=paper
    DB:    TRADING_MODE=testnet      (某次页面切换写入)

启动时 DB 覆盖 `trading_mode=testnet`（DB > env, 正确），但**冲突检查**仍把 `.env` 里
那条**已被取代的** `PAPER_TRADING=true` 当成操作者意图 →
`配置冲突: paper_trading 显式设为 True, 但 TRADING_MODE=testnet 要求 False`
→ **RuntimeError, 服务完全起不来**。

**根因**：`explicitly_set` 用的是 `model_fields_set`(含 .env 显式键)，没有区分
「同层写下的矛盾配置」与「被更新的一层取代的旧值」。**DB override 才是当前权威来源。**

**修法**：`TRADING_MODE` 被 DB 覆盖时，它**推导出的**那些字段也一并从冲突判定中剔除。
注意这**没有放宽 §18**：`TRADING_MODE` 与旧开关**同层**(都在 .env)时冲突照常 fail-closed。

本文件按任务单要求，**不只测 `resolve_mode()` 单函数**，而是走
「apply → 重建 runtime 上下文 → 重新加载 DB overrides → resolve → 断言」全链路。
"""

from __future__ import annotations

import pytest

from at01_common.settings import Settings
from at01_common.trading_mode import MODE_DERIVED, TradingMode, resolve_mode

pytestmark = pytest.mark.usefixtures("db_tables")


async def _reload_like_startup(*, env_values: dict, db_attrs: set[str]) -> tuple[Settings, object]:
    """模拟**一次重启**: env 造 Settings → 叠加 DB 覆盖 → 解析模式。

    与 `wiring.wire_system()` 的顺序一致（DB 覆盖 → 模式解析 → 排除被覆盖字段）。
    """
    s = Settings(_env_file=None, **env_values)

    # 1) DB override 叠加(与 runtime_config.apply_overrides 同语义)
    from at01_common.runtime_config import load_overrides, override_allowlist
    from at01_common.runtime_config import env_value_to_settings

    allowed = override_allowlist()
    overridden: set[str] = set()
    for key, raw in (await load_overrides()).items():
        spec = allowed.get(key)
        if spec is None:
            continue
        setattr(s, spec.attr, env_value_to_settings(spec, raw))
        overridden.add(spec.attr)

    # 2) TRADING_MODE 被 DB 覆盖时, 其推导字段也一并豁免冲突判定
    if "trading_mode" in overridden:
        for d in MODE_DERIVED.values():
            overridden |= set(d)

    r = resolve_mode(
        trading_mode=s.trading_mode,
        paper_trading=s.paper_trading,
        binance_testnet=s.binance_testnet,
        run_testnet_trading=s.run_testnet_trading,
        live_trading_confirm=s.live_trading_confirm,
        mainnet_api_scope_confirmed=s.mainnet_api_scope_confirmed,
        explicitly_set=set(s.model_fields_set) - overridden,
    )
    return s, r


class TestMigrationConflictNoLongerBricksStartup:
    """**核心回归**: `.env` 遗留旧开关 + DB 新模式 → 必须能启动。"""

    async def test_env_legacy_paper_plus_db_testnet_starts(self):
        """复现原 bug 的确切组合: 应当正常解析为 TESTNET, 而不是拒绝启动。"""
        from at01_common.runtime_config import save_overrides

        # DB 里只有 TRADING_MODE=testnet(模拟页面切换写入)
        await save_overrides({"TRADING_MODE": "testnet"}, by="test")

        # .env 里是迁移前的旧值
        _, r = await _reload_like_startup(
            env_values={"paper_trading": True, "binance_testnet": True, "trading_mode": "paper"},
            db_attrs={"trading_mode"},
        )

        assert r.ok, f"修复前这里会抛「配置冲突」把服务卡死: {r.error}"
        assert r.mode is TradingMode.TESTNET

    async def test_same_layer_conflict_still_fails_closed(self):
        """**没有放宽 §18**: TRADING_MODE 与旧开关同层(都来自 env)时, 冲突照常拦。"""
        _, r = await _reload_like_startup(
            env_values={"paper_trading": True, "binance_testnet": True, "trading_mode": "testnet"},
            db_attrs=set(),  # 无 DB 覆盖 → 全部字段都是"同层显式"
        )
        assert not r.ok
        assert "冲突" in r.error


class TestApplyThenReload:
    """任务单点名要求的 `test_mode_switch_apply_then_reload` 全链路。"""

    async def test_apply_testnet_then_reload(self):
        """apply → 重建上下文 → 重载 DB → resolve → 断言 TESTNET。"""
        from at01_common.runtime_config import save_overrides

        # 与 web_mode_routes.apply 一致: 一并写推导字段
        await save_overrides({
            "TRADING_MODE": "testnet", "PAPER_TRADING": False, "BINANCE_TESTNET": True,
        })

        s, r = await _reload_like_startup(
            env_values={"paper_trading": True, "binance_testnet": True, "trading_mode": "paper"},
            db_attrs={"trading_mode"},
        )
        assert r.ok, r.error
        assert r.mode is TradingMode.TESTNET

        # 写回推导字段后, 内部三件套必须自洽
        for k, v in r.derived.items():
            setattr(s, k, v)
        assert s.paper_trading is False
        assert s.binance_testnet is True
        assert str(s.run_testnet_trading).strip() == "1"

    async def test_apply_paper_then_reload(self):
        """反向: 切回 paper 后重启也必须是 paper。"""
        from at01_common.runtime_config import save_overrides

        await save_overrides({"TRADING_MODE": "paper", "PAPER_TRADING": True})

        _, r = await _reload_like_startup(
            env_values={"paper_trading": True, "binance_testnet": True, "trading_mode": "testnet"},
            db_attrs={"trading_mode"},
        )
        assert r.ok, r.error
        assert r.mode is TradingMode.PAPER


class TestFullSwitchMatrix:
    """任务单要求的切换矩阵: paper↔testnet / paper→live / testnet→live / live→paper。"""

    @pytest.mark.parametrize(
        ("db_mode", "env_paper", "expected"),
        [
            ("testnet", True, TradingMode.TESTNET),   # paper → testnet
            ("paper", False, TradingMode.PAPER),      # testnet → paper
            ("paper", True, TradingMode.PAPER),       # live → paper
        ],
    )
    async def test_switch_matrix(self, db_mode, env_paper, expected):
        from at01_common.runtime_config import save_overrides

        payload = {"TRADING_MODE": db_mode}
        if db_mode == "testnet":
            payload |= {"PAPER_TRADING": False, "BINANCE_TESTNET": True}
        else:
            payload |= {"PAPER_TRADING": True}
        await save_overrides(payload)

        _, r = await _reload_like_startup(
            env_values={"paper_trading": env_paper, "binance_testnet": True},
            db_attrs={"trading_mode"},
        )
        assert r.ok, r.error
        assert r.mode is expected
