"""V12.7 模式切换 API —— 回归锚定

对应任务单 §19/§20/§21:

- `GET /api/trading-mode` 只读列出三个模式及可启动性;
- `POST /api/trading-mode/preview` **不写任何配置**, 只给 diff + 守卫预检;
- `POST /api/trading-mode/apply` 只保存, **不重启进程**;
- 实盘需要**显式确认**, 未确认时展示真实资金风控参数且拒绝落盘;
- 写接口复用既有 admin 鉴权(不另起一套权限)。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from at90_web.web_app import app

pytestmark = pytest.mark.usefixtures("db_tables")


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def admin_headers(monkeypatch):
    from at01_common.settings import get_settings

    monkeypatch.setenv("WEB_ADMIN_AUTH", "on")
    monkeypatch.setenv("WEB_ADMIN_TOKEN", "mode-test-token")
    get_settings.cache_clear()
    yield {"X-Admin-Token": "mode-test-token"}
    get_settings.cache_clear()


class TestListModes:
    def test_lists_exactly_three_modes(self, client):
        r = client.get("/api/trading-mode")
        assert r.status_code == 200
        body = r.json()
        assert [m["id"] for m in body["modes"]] == ["paper", "testnet", "live"]
        assert [m["label"] for m in body["modes"]] == ["模拟", "测试网", "实盘"]

    def test_reports_current_mode_and_market_source(self, client):
        body = client.get("/api/trading-mode").json()
        assert body["current"]["mode"] in ("paper", "testnet", "live")
        assert body["current"]["market_data_source"] in ("testnet", "mainnet")

    def test_live_always_needs_confirmation(self, client):
        """实盘在列表里必须标为「需确认」—— 不能给人"点一下就进实盘"的错觉。"""
        live = next(m for m in client.get("/api/trading-mode").json()["modes"]
                    if m["id"] == "live")
        assert live["startable"] is False
        assert any("确认" in r for r in live["blocked_reasons"])


class TestPreview:
    def test_preview_writes_nothing(self, client, admin_headers):
        """预览是**只读**的 —— 不能因为点了预览就把配置改了。"""
        import anyio

        from at01_common.runtime_config import load_overrides

        client.post("/api/trading-mode/preview", json={"mode": "testnet"},
                    headers=admin_headers)
        assert anyio.run(load_overrides) == {}

    def test_preview_shows_diff(self, client, admin_headers):
        d = client.post("/api/trading-mode/preview", json={"mode": "testnet"},
                        headers=admin_headers).json()
        assert d["target_label"] == "测试网"
        assert d["requires_restart"] is True
        changed = {c["attr"]: (c["from"], c["to"]) for c in d["changes"] if c["changed"]}
        assert changed["paper_trading"] == (True, False)

    def test_preview_live_without_confirm_shows_risk_and_blocks(self, client, admin_headers):
        d = client.post("/api/trading-mode/preview", json={"mode": "live"},
                        headers=admin_headers).json()
        assert d["ok"] is False
        assert d["requires_live_confirmation"] is True
        # §11: 确认页必须展示真实资金相关参数
        assert d["risk_summary"], "实盘确认页必须列出风控参数"
        assert d["market_data_source"] == "mainnet"

    def test_preview_live_with_confirm_passes_guards(self, client, admin_headers):
        """带确认后实盘应通过守卫 —— 主网门槛由主网守卫把关(确认 + 九项自检)。"""
        d = client.post("/api/trading-mode/preview",
                        json={"mode": "live", "live_confirmed": True},
                        headers=admin_headers).json()
        assert d["requires_live_confirmation"] is False
        assert d["ok"] is True, d["guard"]

    def test_preview_unknown_mode_rejected(self, client, admin_headers):
        d = client.post("/api/trading-mode/preview", json={"mode": "prod"},
                        headers=admin_headers).json()
        assert d["ok"] is False and "未知模式" in d["error"]


class TestApply:
    def test_apply_requires_auth(self, client, monkeypatch):
        """复用既有 admin 写鉴权(§19), 不另起一套。"""
        from at01_common.settings import get_settings

        monkeypatch.setenv("WEB_ADMIN_AUTH", "on")
        monkeypatch.setenv("WEB_ADMIN_TOKEN", "mode-test-token")
        get_settings.cache_clear()
        try:
            assert client.post("/api/trading-mode/apply",
                               json={"mode": "testnet"}).status_code == 401
        finally:
            get_settings.cache_clear()

    def test_apply_live_without_confirm_refused(self, client, admin_headers):
        """实盘确认是**人工动作** —— 没确认就拒绝落盘, 且把参数摆出来。"""
        import anyio

        from at01_common.runtime_config import load_overrides

        d = client.post("/api/trading-mode/apply", json={"mode": "live"},
                        headers=admin_headers).json()
        assert d["ok"] is False and d["stage"] == "confirm"
        assert d["risk_summary"]
        assert anyio.run(load_overrides) == {}, "拒绝时必须一个字节都没写"

    def test_apply_testnet_saves_without_restarting(self, client, admin_headers):
        """§20: apply 只保存, 不自己重启进程。"""
        d = client.post("/api/trading-mode/apply", json={"mode": "testnet"},
                        headers=admin_headers).json()
        assert d["ok"] is True and d["stage"] == "saved"
        assert d["requires_restart"] is True
        assert "重启" in d["message"]
        # 当前运行模式**不变** —— 只有重启才切换
        assert d["target_mode"] == "testnet"

        import anyio

        from at01_common.runtime_config import load_overrides

        assert anyio.run(load_overrides).get("TRADING_MODE") == "testnet"

    def test_apply_live_with_confirm_writes_both_confirms(self, client, admin_headers):
        """实盘确认落地为两个确认标志 —— 那正是"人工确认"的凭据。"""
        d = client.post("/api/trading-mode/apply",
                        json={"mode": "live", "live_confirmed": True},
                        headers=admin_headers).json()
        assert d["ok"] is True, d

        import anyio

        from at01_common.runtime_config import load_overrides

        saved = anyio.run(load_overrides)
        assert saved["TRADING_MODE"] == "live"
        assert saved["LIVE_TRADING_CONFIRM"] == "true"
        assert saved["MAINNET_API_SCOPE_CONFIRM"] == "true"
