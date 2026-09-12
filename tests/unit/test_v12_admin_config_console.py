"""管理页面 / 配置控制台测试(V12.3)。

覆盖任务单 P9 要求:
- `GET /api/admin/config` 未启动时也能返回配置摘要;
- 配置草稿 diff 正确隐藏敏感值;
- 四种模式切换 draft 输出正确;
- 非法百分比 / 非法阈值 / 非法止盈阶梯会被拒绝;
- 主网真实模式不能绕过 `LIVE_TRADING_CONFIRM` 与 `MAINNET_API_SCOPE_CONFIRM`。

以及配置写入的安全性质: 保留注释与顺序、只改目标键、写前备份、可回滚、保留权限位。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from at01_common.config_store import (
    FIELD_SPECS,
    SPECS_BY_KEY,
    build_config_view,
    build_draft,
    changed_env_values,
    config_path_status,
    is_sensitive_key,
    list_backups,
    mask_value,
    proposed_env_values,
    read_env_file,
    resolve_config_path,
    rollback,
    write_env_values,
)
from at01_common.settings import Settings

SAMPLE_ENV = """\
# 生产配置(注释必须保留)
PAPER_TRADING=true
BINANCE_TESTNET=true
# 密钥区
BINANCE_API_KEY=real-key-must-never-leak
WEB_ADMIN_TOKEN=super-secret-token
RISK_MAX_SINGLE_ORDER_PCT=0.05
ENTRY_BUY_THRESHOLD=80
"""


def _settings(**overrides: Any) -> Settings:
    """确定性 settings(不依赖仓库 .env 的具体内容)。"""
    base: dict[str, Any] = dict(
        paper_trading=True,
        binance_testnet=True,
        live_trading_confirm="",
        mainnet_api_scope_confirmed=False,
        web_admin_token="super-secret-token",
        symbols="SOLUSDT",
        risk_initial_equity=100000.0,
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    p = tmp_path / "production.env"
    p.write_text(SAMPLE_ENV, encoding="utf-8")
    os.chmod(p, 0o600)
    return p


# ---------------------------------------------------------------------------
# 敏感判定与掩码
# ---------------------------------------------------------------------------


class TestSensitive:
    @pytest.mark.parametrize("key", [
        "BINANCE_API_KEY", "BINANCE_API_SECRET", "WEB_ADMIN_TOKEN",
        "BINANCE_TESTNET_API_KEY", "AI_API_KEY", "SOME_PASSWORD",
    ])
    def test_sensitive_keys_detected(self, key):
        assert is_sensitive_key(key)

    @pytest.mark.parametrize("key", ["PAPER_TRADING", "RISK_MAX_DRAWDOWN", "LOG_LEVEL"])
    def test_non_sensitive_keys(self, key):
        assert not is_sensitive_key(key)

    def test_mask_shape(self):
        assert mask_value("anything") == "<configured>"
        assert mask_value("") == "<empty>"
        assert mask_value("   ") == "<empty>"

    def test_sensitive_specs_are_not_editable(self):
        for spec in FIELD_SPECS:
            if is_sensitive_key(spec.key):
                assert spec.editable is False, spec.key


# ---------------------------------------------------------------------------
# 配置视图
# ---------------------------------------------------------------------------


class TestConfigView:
    def test_view_masks_token_and_never_leaks(self, env_file, tmp_path):
        view = build_config_view(_settings(), env_file)
        blob = repr(view)
        assert "super-secret-token" not in blob
        assert "real-key-must-never-leak" not in blob
        token_field = next(f for f in view["fields"] if f["key"] == "WEB_ADMIN_TOKEN")
        assert token_field["value"] == "<configured>"
        assert token_field["editable"] is False

    def test_view_has_four_modes(self, env_file):
        view = build_config_view(_settings(), env_file)
        assert {m["id"] for m in view["modes"]} == {
            "paper", "live_testnet", "paper_mainnet", "live_mainnet"}

    def test_view_works_without_file(self, tmp_path):
        """系统未启动 / 文件不存在时也要返回可读摘要。"""
        view = build_config_view(_settings(), tmp_path / "nope.env")
        assert view["fields"]
        assert view["config"]["exists"] is False

    def test_pct_fields_exposed_as_percent(self, env_file):
        """百分比参数必须以百分数呈现, 不让用户猜 0.05 的含义。"""
        view = build_config_view(_settings(), env_file)
        f = next(x for x in view["fields"] if x["key"] == "RISK_MAX_SINGLE_ORDER_PCT")
        assert f["value"] == pytest.approx(5.0)
        assert f["unit"] == "%"

    def test_pending_restart_detected(self, env_file):
        """文件值与运行值不同 → 标记待重启。"""
        view = build_config_view(_settings(entry_buy_threshold=70), env_file)
        assert "ENTRY_BUY_THRESHOLD" in view["pending_restart"]

    def test_no_false_pending_restart_for_matching_pct(self, env_file):
        """回归: 文件与运行值一致时**不得**误报待重启。

        `_settings_to_ui` 曾绕道 `_to_env`(假定 UI 域)再做一次 /100, 把 0.05 变成 0.0005,
        导致所有百分比参数都被误判为「与文件不一致」。
        """
        view = build_config_view(_settings(risk_max_single_order_pct=0.05), env_file)
        f = next(x for x in view["fields"] if x["key"] == "RISK_MAX_SINGLE_ORDER_PCT")
        assert f["live_value"] == pytest.approx(5.0)
        assert "RISK_MAX_SINGLE_ORDER_PCT" not in view["pending_restart"]

    def test_live_value_units_match_file_units(self, env_file):
        """live_value 与 value 必须同一量纲(百分数), 否则前端无法比较。"""
        view = build_config_view(_settings(
            risk_max_drawdown=0.15, buy_dip_pct=0.005,
            risk_max_single_order_pct=0.05, entry_buy_threshold=80), env_file)
        by_key = {x["key"]: x for x in view["fields"]}
        assert by_key["RISK_MAX_DRAWDOWN"]["live_value"] == pytest.approx(15.0)
        assert by_key["BUY_DIP_PCT"]["live_value"] == pytest.approx(0.5)
        assert by_key["RISK_MAX_SINGLE_ORDER_PCT"]["live_value"] == pytest.approx(5.0)
        assert by_key["ENTRY_BUY_THRESHOLD"]["live_value"] == pytest.approx(80.0)

    def test_confirm_and_bool_live_values(self, env_file):
        view = build_config_view(_settings(live_trading_confirm="true", paper_trading=True), env_file)
        by_key = {x["key"]: x for x in view["fields"]}
        assert by_key["LIVE_TRADING_CONFIRM"]["live_value"] is True
        assert by_key["PAPER_TRADING"]["live_value"] is True


# ---------------------------------------------------------------------------
# 草稿: 四种模式
# ---------------------------------------------------------------------------


class TestDraftModes:
    @pytest.mark.parametrize(
        ("changes", "expected"),
        [
            ({"PAPER_TRADING": True, "BINANCE_TESTNET": True}, "paper"),
            ({"PAPER_TRADING": False, "BINANCE_TESTNET": True}, "live_testnet"),
        ],
    )
    def test_two_startable_modes(self, env_file, changes, expected):
        d = build_draft(base_settings=_settings(), proposed=changes, path=env_file)
        assert d["result_mode"] == expected
        assert d["ok"] is True, d
        # 有实际改动才需要重启(提交值与文件一致时 diff 为空)
        assert d["requires_restart"] == bool(d["diff"])

    def test_paper_mainnet_is_blocked_by_guards(self, env_file):
        """**守卫行为记录**: 「主网纸面观察」在当前安全守卫下无法启动。

        两道守卫都会拦: ①连主网一律要求 LIVE_TRADING_CONFIRM=true(即便纸面);
        ②主网就绪自检要求 PAPER_TRADING=false。管理页面不绕过, 只如实标注。
        """
        d = build_draft(base_settings=_settings(), proposed={
            "PAPER_TRADING": True, "BINANCE_TESTNET": False}, path=env_file)
        assert d["ok"] is False
        assert d["blocked_reasons"]
        # 该模式在配置视图里必须被标为不可用
        view = build_config_view(_settings(), env_file)
        pm = next(m for m in view["modes"] if m["id"] == "paper_mainnet")
        assert pm["blocked"] is True
        assert pm["note"]

    def test_live_mainnet_requires_both_confirmations(self, env_file):
        """主网真实: 缺 LIVE_TRADING_CONFIRM 或 MAINNET_API_SCOPE_CONFIRM 都必须被拦。"""
        # 1) 两个确认都缺
        d = build_draft(base_settings=_settings(), proposed={
            "PAPER_TRADING": False, "BINANCE_TESTNET": False}, path=env_file)
        assert d["ok"] is False
        assert any("LIVE_TRADING_CONFIRM" in r for r in d["blocked_reasons"])

        # 2) 只给 LIVE_TRADING_CONFIRM(仍缺 API 权限确认)
        d2 = build_draft(base_settings=_settings(), proposed={
            "PAPER_TRADING": False, "BINANCE_TESTNET": False,
            "LIVE_TRADING_CONFIRM": True}, path=env_file)
        assert d2["ok"] is False
        assert any("MAINNET_API_SCOPE_CONFIRM" in r for r in d2["blocked_reasons"])

        # 3) 两个确认都给 → 放行
        d3 = build_draft(base_settings=_settings(), proposed={
            "PAPER_TRADING": False, "BINANCE_TESTNET": False,
            "LIVE_TRADING_CONFIRM": True, "MAINNET_API_SCOPE_CONFIRM": True}, path=env_file)
        assert d3["result_mode"] == "live_mainnet"
        assert d3["ok"] is True, d3

    def test_confirm_field_stays_string_in_candidate(self, env_file):
        """回归: `live_trading_confirm` 在 Settings 里是 str, 不能塞布尔值。

        塞布尔会让 `mainnet_blocked_reason()` 的 `.strip()` 抛 AttributeError。
        """
        d = build_draft(base_settings=_settings(), proposed={
            "PAPER_TRADING": False, "BINANCE_TESTNET": False,
            "LIVE_TRADING_CONFIRM": True, "MAINNET_API_SCOPE_CONFIRM": True}, path=env_file)
        assert d["ok"] is True, d  # 未抛异常即已通过守卫
        from at01_common.config_store import _ui_to_settings

        spec = SPECS_BY_KEY["LIVE_TRADING_CONFIRM"]
        assert _ui_to_settings(spec, True) == "true"
        assert _ui_to_settings(spec, False) == ""

    def test_pct_ui_value_converts_to_fraction(self, env_file):
        """回归: UI 的 3 表示 3%, 进入 Settings 前必须换算成 0.03。"""
        from at01_common.config_store import _ui_to_settings

        spec = SPECS_BY_KEY["RISK_MAX_SINGLE_ORDER_PCT"]
        assert _ui_to_settings(spec, 3) == pytest.approx(0.03)
        assert _ui_to_settings(spec, 0.5) == pytest.approx(0.005)
        # 不换算会把 3.0 当作 300% 塞进 Settings
        d = build_draft(base_settings=_settings(),
                        proposed={"RISK_MAX_SINGLE_ORDER_PCT": 3}, path=env_file)
        assert d["ok"] is True, d

    def test_live_mainnet_emits_danger_warning(self, env_file):
        d = build_draft(base_settings=_settings(), proposed={
            "PAPER_TRADING": False, "BINANCE_TESTNET": False,
            "LIVE_TRADING_CONFIRM": True, "MAINNET_API_SCOPE_CONFIRM": True}, path=env_file)
        assert any(w["level"] == "danger" for w in d["risk_warnings"])
        assert any("真实资金" in w["text"] for w in d["risk_warnings"])

    def test_leaving_mainnet_clears_nothing_implicitly(self, env_file):
        """draft 只反映提交的改动, 不会隐式改写确认字段。"""
        d = build_draft(base_settings=_settings(live_trading_confirm="true"),
                        proposed={"BINANCE_TESTNET": True}, path=env_file)
        keys = {x["key"] for x in d["diff"]}
        assert "LIVE_TRADING_CONFIRM" not in keys


# ---------------------------------------------------------------------------
# 草稿: 拒绝非法输入
# ---------------------------------------------------------------------------


class TestDraftRejects:
    def test_illegal_percent_zero(self, env_file):
        d = build_draft(base_settings=_settings(),
                        proposed={"RISK_MAX_SINGLE_ORDER_PCT": 0}, path=env_file)
        assert d["ok"] is False
        assert any(p["key"] == "RISK_MAX_SINGLE_ORDER_PCT" for p in d["problems"])

    def test_illegal_percent_over_100(self, env_file):
        d = build_draft(base_settings=_settings(),
                        proposed={"RISK_MAX_DRAWDOWN": 500}, path=env_file)
        assert d["ok"] is False

    def test_illegal_threshold_ordering(self, env_file):
        """观察阈值必须小于买入阈值(由 settings.validate() 兜底)。"""
        d = build_draft(base_settings=_settings(), proposed={
            "ENTRY_OBSERVE_THRESHOLD": 90, "ENTRY_BUY_THRESHOLD": 70}, path=env_file)
        assert d["ok"] is False
        assert any("买入阈值" in p["message"] for p in d["problems"])

    @pytest.mark.parametrize("bad", ["", "abc", "5", "5:0", "0:20", "5:200", "5:"])
    def test_illegal_ladder(self, env_file, bad):
        d = build_draft(base_settings=_settings(),
                        proposed={"SELL_TAKE_PROFIT_LADDER": bad}, path=env_file)
        assert d["ok"] is False, bad

    def test_valid_ladder_accepted(self, env_file):
        d = build_draft(base_settings=_settings(),
                        proposed={"SELL_TAKE_PROFIT_LADDER": "5:20,10:30,20:50"}, path=env_file)
        assert d["ok"] is True

    def test_rejects_sensitive_field(self, env_file):
        d = build_draft(base_settings=_settings(),
                        proposed={"BINANCE_API_KEY": "hacked"}, path=env_file)
        assert d["ok"] is False
        assert any(p["key"] == "BINANCE_API_KEY" for p in d["problems"])
        assert "hacked" not in repr(d)

    def test_rejects_readonly_field(self, env_file):
        d = build_draft(base_settings=_settings(),
                        proposed={"SYMBOLS": "BTCUSDT"}, path=env_file)
        assert d["ok"] is False
        assert any(p["key"] == "SYMBOLS" for p in d["problems"])

    def test_rejects_unknown_field(self, env_file):
        d = build_draft(base_settings=_settings(),
                        proposed={"TOTALLY_MADE_UP": 1}, path=env_file)
        assert d["ok"] is False

    def test_rejects_non_numeric(self, env_file):
        d = build_draft(base_settings=_settings(),
                        proposed={"RECONCILE_INTERVAL_SECONDS": "abc"}, path=env_file)
        assert d["ok"] is False


# ---------------------------------------------------------------------------
# 草稿: diff 与敏感值
# ---------------------------------------------------------------------------


class TestDraftDiff:
    def test_diff_has_no_secret_values(self, env_file):
        d = build_draft(base_settings=_settings(),
                        proposed={"RISK_MAX_SINGLE_ORDER_PCT": 3}, path=env_file)
        blob = repr(d)
        assert "super-secret-token" not in blob
        assert "real-key-must-never-leak" not in blob

    def test_diff_only_contains_changed_keys(self, env_file):
        d = build_draft(base_settings=_settings(),
                        proposed={"RISK_MAX_SINGLE_ORDER_PCT": 5}, path=env_file)
        # 提交值与文件一致 → 无 diff
        assert d["diff"] == []

    def test_diff_reports_from_and_to(self, env_file):
        d = build_draft(base_settings=_settings(),
                        proposed={"RISK_MAX_SINGLE_ORDER_PCT": 3}, path=env_file)
        row = next(x for x in d["diff"] if x["key"] == "RISK_MAX_SINGLE_ORDER_PCT")
        assert row["from"] == "5.0%"
        assert row["to"] == "3.0%"
        assert row["sensitive"] is False

    def test_percent_round_trip_to_env_value(self):
        assert proposed_env_values({"RISK_MAX_SINGLE_ORDER_PCT": 3}) == {
            "RISK_MAX_SINGLE_ORDER_PCT": "0.03"}
        assert proposed_env_values({"BUY_DIP_PCT": 0.5}) == {"BUY_DIP_PCT": "0.005"}

    def test_changed_env_values_filters_identical(self, env_file):
        assert changed_env_values(env_file, {"PAPER_TRADING": "true"}) == {}
        assert changed_env_values(env_file, {"PAPER_TRADING": "false"}) == {
            "PAPER_TRADING": "false"}


# ---------------------------------------------------------------------------
# 写入 / 备份 / 回滚
# ---------------------------------------------------------------------------


class TestWriteAndRollback:
    def test_write_preserves_comments_and_order(self, env_file):
        write_env_values(env_file, {"PAPER_TRADING": "false"})
        text = env_file.read_text(encoding="utf-8")
        assert "# 生产配置(注释必须保留)" in text
        assert "# 密钥区" in text
        assert "PAPER_TRADING=false" in text
        # 其它键未被动过
        assert "BINANCE_API_KEY=real-key-must-never-leak" in text
        assert text.index("PAPER_TRADING") < text.index("BINANCE_TESTNET")

    def test_write_creates_backup(self, env_file):
        assert list_backups(env_file) == []
        result = write_env_values(env_file, {"PAPER_TRADING": "false"})
        assert result["backup"]
        backups = list_backups(env_file)
        assert len(backups) == 1
        assert "PAPER_TRADING=true" in backups[0].read_text(encoding="utf-8")

    def test_write_appends_missing_key(self, env_file):
        result = write_env_values(env_file, {"LOG_LEVEL": "DEBUG"})
        assert result["appended"] == ["LOG_LEVEL"]
        assert read_env_file(env_file)["LOG_LEVEL"] == "DEBUG"

    def test_write_preserves_permissions(self, env_file):
        before = env_file.stat().st_mode & 0o777
        write_env_values(env_file, {"PAPER_TRADING": "false"})
        assert (env_file.stat().st_mode & 0o777) == before

    def test_rollback_restores_previous(self, env_file):
        write_env_values(env_file, {"PAPER_TRADING": "false"})
        assert read_env_file(env_file)["PAPER_TRADING"] == "false"
        result = rollback(env_file)
        assert result["ok"] is True
        assert read_env_file(env_file)["PAPER_TRADING"] == "true"

    def test_save_then_rollback_same_second_keeps_source(self, env_file):
        """回归: 备份时间戳必须能区分同一秒内的两次备份。

        秒级精度下「保存 → 立刻回滚」会生成同名备份并覆盖掉要恢复的那一份,
        导致回滚无效(实测过)。
        """
        write_env_values(env_file, {"PAPER_TRADING": "false"})
        write_env_values(env_file, {"PAPER_TRADING": "false", "LOG_LEVEL": "DEBUG"})
        backups = list_backups(env_file)
        assert len(backups) == 2, backups
        assert len({b.name for b in backups}) == 2

    def test_backup_names_are_unique_under_rapid_writes(self, env_file):
        for i in range(5):
            write_env_values(env_file, {"ENTRY_BUY_THRESHOLD": str(60 + i)})
        names = [b.name for b in list_backups(env_file)]
        assert len(names) == len(set(names))

    def test_rollback_without_backup(self, tmp_path):
        p = tmp_path / "fresh.env"
        p.write_text("A=1\n", encoding="utf-8")
        assert rollback(p)["ok"] is False

    def test_backup_prunes_old(self, env_file, monkeypatch):
        from at01_common import config_store

        monkeypatch.setattr(config_store, "BACKUP_KEEP", 2)
        for i in range(4):
            write_env_values(env_file, {"ENTRY_BUY_THRESHOLD": str(70 + i)})
        assert len(list_backups(env_file)) <= 2

    def test_no_partial_write_on_missing_dir(self, tmp_path):
        """目录不存在时应抛错而不是静默成功。"""
        p = tmp_path / "nope" / "x.env"
        with pytest.raises(OSError):
            write_env_values(p, {"A": "1"})

    def test_config_path_status_reports_writability(self, env_file, tmp_path):
        assert config_path_status(env_file)["writable"] is True
        assert config_path_status(tmp_path / "missing" / "x.env")["writable"] is False


class TestResolvePath:
    def test_env_var_wins(self, monkeypatch):
        monkeypatch.setenv("ADAPTIVE_TRADING_ENV_FILE", "/etc/adaptive-trading/production.env")
        assert str(resolve_config_path()).endswith("production.env")

    def test_defaults_to_dot_env(self, monkeypatch):
        monkeypatch.delenv("ADAPTIVE_TRADING_ENV_FILE", raising=False)
        assert str(resolve_config_path()).replace("\\", "/") == ".env"


# ---------------------------------------------------------------------------
# API 层
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    from at90_web import app, system_state

    saved = system_state.running
    system_state.running = True
    yield TestClient(app)
    system_state.running = saved


@pytest.fixture
def admin_headers(monkeypatch):
    from at01_common.settings import get_settings

    monkeypatch.setenv("WEB_ADMIN_TOKEN", "test-token-123")
    get_settings.cache_clear()
    yield {"X-Admin-Token": "test-token-123"}
    get_settings.cache_clear()


@pytest.fixture
def cfg_env(monkeypatch, tmp_path):
    """把配置路径指向临时文件(避免动到仓库 .env)。"""
    p = tmp_path / "production.env"
    p.write_text(SAMPLE_ENV, encoding="utf-8")
    monkeypatch.setenv("ADAPTIVE_TRADING_ENV_FILE", str(p))
    return p


class TestAdminAPI:
    def test_admin_page_served(self, client):
        r = client.get("/admin")
        assert r.status_code == 200
        assert "管理" in r.text

    def test_config_readable_without_token(self, client, cfg_env):
        r = client.get("/api/admin/config")
        assert r.status_code == 200
        body = r.json()
        assert body["fields"]
        assert body["modes"]
        assert "super-secret-token" not in r.text
        assert "real-key-must-never-leak" not in r.text

    def test_draft_requires_token(self, client, cfg_env, monkeypatch):
        from at01_common.settings import get_settings
        monkeypatch.setenv("WEB_ADMIN_TOKEN", "test-token-123")
        get_settings.cache_clear()
        try:
            r = client.post("/api/admin/config/draft", json={"changes": {"PAPER_TRADING": False}})
            assert r.status_code == 401
        finally:
            get_settings.cache_clear()

    def test_draft_with_token(self, client, cfg_env, admin_headers):
        r = client.post("/api/admin/config/draft",
                        json={"changes": {"RISK_MAX_SINGLE_ORDER_PCT": 3}},
                        headers=admin_headers)
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_apply_writes_to_database(self, client, cfg_env, admin_headers, db_tables):
        """V12.6 P1 起, 可编辑字段写入**数据库**(不再写 env 文件)。

        行为按设计变更: 好处是 Pi 上不再需要挂载可写配置目录(DB 本就在持久化卷上)。
        原断言「写文件 + 留备份」改为「入 DB + 有审计」——
        `runtime_config_history` 承担了原先文件备份的追溯职责。
        """
        r = client.post("/api/admin/config/apply",
                        json={"changes": {"RISK_MAX_SINGLE_ORDER_PCT": 3}},
                        headers=admin_headers)
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["requires_restart"] is True
        assert "RISK_MAX_SINGLE_ORDER_PCT" in body["storage"]["database"]
        assert body["storage"]["file"] == []
        # env 文件不应被这次写入改动
        assert read_env_file(cfg_env)["RISK_MAX_SINGLE_ORDER_PCT"] == "0.05"

    def test_apply_rejects_mainnet_without_confirms(self, client, cfg_env, admin_headers):
        """核心安全断言: 管理页面无法绕过主网守卫。"""
        r = client.post("/api/admin/config/apply",
                        json={"changes": {"PAPER_TRADING": False, "BINANCE_TESTNET": False}},
                        headers=admin_headers)
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is False
        assert body["stage"] == "validate"
        # 文件必须没被改动
        assert read_env_file(cfg_env)["BINANCE_TESTNET"] == "true"

    def test_apply_works_without_writable_config_file(
        self, client, tmp_path, admin_headers, monkeypatch, db_tables
    ):
        """**P1 的核心收益**: 配置文件不可写时, 可编辑字段照样能保存(走 DB)。

        原断言是「不可写 → 409 stage=write」; 行为按设计变更 ——
        全部可编辑字段都已入库, 文件只承载密钥等 bootstrap 关键项(且它们不可编辑,
        不会出现在 changes 里), 所以 Pi 上不需要再 `chown` 配置目录。
        """
        monkeypatch.setenv("ADAPTIVE_TRADING_ENV_FILE",
                           str(tmp_path / "missing" / "production.env"))
        r = client.post("/api/admin/config/apply",
                        json={"changes": {"PAPER_TRADING": False}},
                        headers=admin_headers)
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert "PAPER_TRADING" in body["storage"]["database"]

    def test_rollback_endpoint(self, client, cfg_env, admin_headers, db_tables):
        """V12.6 P1: 回滚按审计**逐步回退数据库覆盖**(原为恢复 env 文件备份)。

        行为按设计变更: 配置改存 DB 后, 只恢复文件会让覆盖活下来 ——
        操作者点了「恢复上一份配置」却发现值没回去。
        """
        client.post("/api/admin/config/apply",
                    json={"changes": {"RISK_MAX_SINGLE_ORDER_PCT": 3}},
                    headers=admin_headers)
        r = client.post("/api/admin/config/rollback", headers=admin_headers)
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert r.json()["database_reverted"]["RISK_MAX_SINGLE_ORDER_PCT"] is None  # 原值即无覆盖

        # 回退的实质 = DB 里那条覆盖没了, 下次启动自然落回 env 值(0.05)。
        # 注意: 进程内的 settings 单例不会自己变回去 —— 重启才生效, 这正是响应里
        # `requires_restart=True` 的含义。
        from at01_common.runtime_config import load_overrides

        import anyio

        assert "RISK_MAX_SINGLE_ORDER_PCT" not in anyio.run(load_overrides)


# ---------------------------------------------------------------------------
# 令牌探针 / 重启服务
# ---------------------------------------------------------------------------


class TestAuthCheck:
    def test_auth_check_401_without_token(self, client, cfg_env, monkeypatch):
        from at01_common.settings import get_settings
        monkeypatch.setenv("WEB_ADMIN_TOKEN", "test-token-123")
        get_settings.cache_clear()
        try:
            assert client.get("/api/admin/auth-check").status_code == 401
            assert client.get("/api/admin/auth-check",
                              headers={"X-Admin-Token": "wrong"}).status_code == 401
            assert client.get("/api/admin/auth-check",
                              headers={"X-Admin-Token": "test-token-123"}).status_code == 200
        finally:
            get_settings.cache_clear()

    def test_auth_check_503_when_token_unset(self, client, cfg_env, monkeypatch):
        from at01_common.settings import get_settings
        monkeypatch.setenv("WEB_ADMIN_TOKEN", "")
        get_settings.cache_clear()
        try:
            # 未配置令牌 → fail-closed, 写接口整体不可用
            assert client.get("/api/admin/auth-check").status_code == 503
        finally:
            get_settings.cache_clear()


class TestRestart:
    def test_restart_requires_token(self, client, cfg_env, monkeypatch):
        from at01_common.settings import get_settings
        monkeypatch.setenv("WEB_ADMIN_TOKEN", "test-token-123")
        get_settings.cache_clear()
        try:
            assert client.post("/api/admin/restart").status_code == 401
        finally:
            get_settings.cache_clear()

    def test_restart_refused_when_config_fails_guards(self, client, tmp_path,
                                                      admin_headers, monkeypatch):
        """核心安全断言: 配置过不了守卫时**拒绝重启**, 免得服务起不来。"""
        bad = tmp_path / "production.env"
        bad.write_text("PAPER_TRADING=true\nBINANCE_TESTNET=false\n", encoding="utf-8")
        monkeypatch.setenv("ADAPTIVE_TRADING_ENV_FILE", str(bad))
        r = client.post("/api/admin/restart", headers=admin_headers)
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is False
        assert body["stage"] == "preflight"
        assert body["blocked_reasons"]

    def test_restart_reports_undeliverable_without_runtime(self, client, cfg_env,
                                                           admin_headers):
        """测试环境没有跑 `runtime.run()`, 投递必然失败 —— 必须如实回报而非假装成功。"""
        r = client.post("/api/admin/restart", headers=admin_headers)
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is False
        assert body["stage"] == "signal"

    def test_shutdown_endpoint_reports_delivery(self, client, cfg_env, admin_headers):
        """V12.3: `/api/shutdown` 此前只置一个没人读的标志(空操作); 现在如实回报投递结果。"""
        from at90_web import system_state

        system_state.extra["shutdown_requested"] = False
        r = client.post("/api/shutdown", headers=admin_headers)
        assert r.status_code == 200
        body = r.json()
        assert system_state.extra["shutdown_requested"] is True
        assert body["ok"] is False          # 测试环境无 run() 主循环
        assert body["msg"]


class TestAuthDisabled:
    """V12.4: `WEB_ADMIN_AUTH=off` —— 个人局域网的显式逃生口。

    默认必须仍然安全(需要令牌); 只有显式关闭才放行, 且必须在界面上暴露出来。
    """

    def test_default_is_auth_on(self):
        s = Settings()
        assert s.admin_auth_disabled is False

    @pytest.mark.parametrize("val", ["off", "OFF", "false", "0", "no", "disabled", "none", " off "])
    def test_disabling_values(self, val):
        assert Settings(web_admin_auth=val).admin_auth_disabled is True

    @pytest.mark.parametrize("val", ["on", "ON", "true", "yes", ""])
    def test_enabling_values(self, val):
        assert Settings(web_admin_auth=val).admin_auth_disabled is False

    def test_validate_still_blocks_non_loopback_without_token(self):
        """默认仍然 fail-fast: 非回环 + 空令牌 → 拒绝启动。"""
        s = Settings(api_host="0.0.0.0", web_admin_token="", web_admin_auth="on")
        problems = s.validate()
        assert any("WEB_ADMIN_TOKEN" in p for p in problems)

    def test_validate_allows_non_loopback_when_auth_explicitly_off(self):
        """显式关闭鉴权后, 非回环 + 空令牌不再拦截。"""
        s = Settings(api_host="0.0.0.0", web_admin_token="", web_admin_auth="off")
        problems = s.validate()
        assert not any("WEB_ADMIN_TOKEN" in p for p in problems)

    def test_write_endpoint_requires_no_token_when_disabled(self, client, cfg_env, monkeypatch):
        from at01_common.settings import get_settings

        monkeypatch.setenv("WEB_ADMIN_TOKEN", "")
        monkeypatch.setenv("WEB_ADMIN_AUTH", "off")
        get_settings.cache_clear()
        try:
            r = client.post("/api/admin/config/draft",
                            json={"changes": {"RISK_MAX_SINGLE_ORDER_PCT": 3}})
            assert r.status_code == 200
            assert r.json()["ok"] is True
        finally:
            get_settings.cache_clear()

    def test_write_endpoint_still_locked_by_default(self, client, cfg_env, monkeypatch):
        """回归: 不显式关闭时, 空令牌仍然是 503(fail-closed)。"""
        from at01_common.settings import get_settings

        monkeypatch.setenv("WEB_ADMIN_TOKEN", "")
        monkeypatch.setenv("WEB_ADMIN_AUTH", "on")
        get_settings.cache_clear()
        try:
            r = client.post("/api/admin/config/draft",
                            json={"changes": {"RISK_MAX_SINGLE_ORDER_PCT": 3}})
            assert r.status_code == 503
        finally:
            get_settings.cache_clear()

    def test_enum_parsing_is_case_insensitive_but_returns_canonical(self, env_file):
        """回归: enum 解析不能无条件 `.upper()`。

        `LOG_LEVEL` 的选项是大写, `WEB_ADMIN_AUTH` 是小写(on/off)。一律转大写会让后者
        永远匹配不上 —— 这个 bug 是在真机上执行 apply 时才暴露的。
        """
        from at01_common.config_store import _parse_field

        assert _parse_field(SPECS_BY_KEY["WEB_ADMIN_AUTH"], "off") == ("off", None)
        assert _parse_field(SPECS_BY_KEY["WEB_ADMIN_AUTH"], "OFF")[0] == "off"
        assert _parse_field(SPECS_BY_KEY["WEB_ADMIN_AUTH"], " on ")[0] == "on"
        assert _parse_field(SPECS_BY_KEY["WEB_ADMIN_AUTH"], "off").__class__ is tuple
        assert _parse_field(SPECS_BY_KEY["LOG_LEVEL"], "debug")[0] == "DEBUG"
        assert _parse_field(SPECS_BY_KEY["LOG_LEVEL"], "WARNING")[0] == "WARNING"
        assert _parse_field(SPECS_BY_KEY["WEB_ADMIN_AUTH"], "maybe")[0] is None

    def test_apply_web_admin_auth_off_round_trip(self, env_file):
        d = build_draft(base_settings=_settings(),
                        proposed={"WEB_ADMIN_AUTH": "off"}, path=env_file)
        assert d["ok"] is True, d
        assert proposed_env_values({"WEB_ADMIN_AUTH": "off"}) == {"WEB_ADMIN_AUTH": "off"}

    def test_config_field_exists_and_warns(self):
        spec = SPECS_BY_KEY["WEB_ADMIN_AUTH"]
        assert spec.editable is True
        assert spec.choices == ("on", "off")
        assert spec.warn_when == "off"
        assert "任何设备" in spec.warn_text

    def test_operator_status_exposes_flag(self, client, cfg_env, monkeypatch):
        from at01_common.settings import get_settings

        monkeypatch.setenv("WEB_ADMIN_AUTH", "off")
        get_settings.cache_clear()
        try:
            body = client.get("/api/operator-status").json()
            assert body["auth_disabled"] is True
            assert body["auth_notice"]
        finally:
            get_settings.cache_clear()

    def test_ops_page_reports_auth_state(self, client):
        r = client.get("/ops")
        assert r.status_code == 200
        assert "WEB_ADMIN_AUTH" in r.text


class TestRuntimeShutdownChannel:
    def test_request_shutdown_without_run_is_false(self):
        from at01_common.runtime import request_shutdown, shutdown_requested

        assert request_shutdown() is False
        assert shutdown_requested() is False

    def test_request_shutdown_delivers_to_registered_event(self):
        """注册了 stop_event 后, 请求必须真的被置位(与 Ctrl+C 同一路径)。"""
        import asyncio

        from at01_common import runtime

        async def main():
            loop = asyncio.get_running_loop()
            ev = asyncio.Event()
            runtime._stop_event = ev
            runtime._stop_loop = loop
            try:
                assert runtime.request_shutdown() is True
                await asyncio.sleep(0)      # 让 call_soon_threadsafe 落地
                assert ev.is_set()
                assert runtime.shutdown_requested() is True
            finally:
                runtime._stop_event = None
                runtime._stop_loop = None

        asyncio.run(main())

    def test_in_container_is_bool(self):
        from at01_common.runtime import in_container

        assert isinstance(in_container(), bool)


# ---------------------------------------------------------------------------
# 字段定义的自洽性
# ---------------------------------------------------------------------------


class TestFieldSpecs:
    def test_keys_unique(self):
        keys = [s.key for s in FIELD_SPECS]
        assert len(keys) == len(set(keys))

    def test_every_spec_maps_to_a_settings_field(self):
        s = Settings()
        for spec in FIELD_SPECS:
            assert hasattr(s, spec.attr), spec.attr

    def test_pct_specs_have_hi_bound(self):
        for spec in FIELD_SPECS:
            if spec.kind in ("pct", "score"):
                assert spec.lo is not None and spec.hi is not None, spec.key

    def test_dead_switch_removed_not_documented(self):
        """V12.6 P3: `MAINNET_READINESS_ENABLED` 已**移除**而非靠文案标注。

        它声明了但全代码库从不被读取 —— 主页面上摆一个点了没反应的开关,
        即便写明「本开关无效」也是坏体验。防回归见 test_v126_dead_config_switches.py。
        """
        assert "MAINNET_READINESS_ENABLED" not in SPECS_BY_KEY
        assert not hasattr(Settings(), "mainnet_readiness_enabled")
