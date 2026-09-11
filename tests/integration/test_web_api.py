"""
Web API 集成测试(TestClient + 内存引擎状态)
"""

import pytest
from fastapi.testclient import TestClient

from at10_web import app, system_state


@pytest.fixture
def client():
    """干净的 TestClient"""
    # 保存/恢复全局状态
    saved = {
        "running": system_state.running,
        "market_engine": system_state.market_engine,
        "analytics_engine": system_state.analytics_engine,
        "strategy_engine": system_state.strategy_engine,
        "risk_manager": system_state.risk_manager,
        "execution_engine": system_state.execution_engine,
    }
    system_state.running = True
    yield TestClient(app)
    for k, v in saved.items():
        setattr(system_state, k, v)


@pytest.fixture
def admin_headers(monkeypatch):
    """V11.5 P0-1: 配置写接口令牌并返回鉴权头, 结束后清缓存恢复。"""
    from at01_common.settings import get_settings

    monkeypatch.setenv("WEB_ADMIN_TOKEN", "test-token-123")
    get_settings.cache_clear()
    yield {"X-Admin-Token": "test-token-123"}
    get_settings.cache_clear()


class TestHealthAndSystem:
    def test_health(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok", "running": True}

    def test_dashboard_html(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "adaptiveTrading" in r.text
        assert "监控面板" in r.text

    def test_system_summary_without_engines(self, client):
        """引擎未注册时系统总览仍可返回"""
        system_state.risk_manager = None
        system_state.execution_engine = None
        system_state.strategy_engine = None
        system_state.market_engine = None
        r = client.get("/api/system")
        assert r.status_code == 200
        body = r.json()
        assert body["running"] is True
        assert body["app"] == "adaptiveTrading"

    def test_market_empty(self, client):
        system_state.market_engine = None
        r = client.get("/api/market")
        assert r.status_code == 200
        assert r.json() == {"symbols": {}}

    def test_analytics_empty(self, client):
        system_state.analytics_engine = None
        r = client.get("/api/analytics")
        assert r.status_code == 200
        assert r.json() == {"symbols": {}, "whales": []}

    def test_risk_empty(self, client):
        system_state.risk_manager = None
        r = client.get("/api/risk")
        assert r.status_code == 200

    def test_breaker_reset_not_running(self, client, admin_headers):
        system_state.risk_manager = None
        r = client.post("/api/breaker/reset", headers=admin_headers)
        assert r.status_code == 200
        assert r.json() == {"ok": False, "msg": "not running"}


class TestWithEngines:
    def test_risk_status(self, client):
        from at60_risk.risk_manager import RiskManager

        system_state.risk_manager = RiskManager()
        r = client.get("/api/risk")
        body = r.json()
        assert "drawdown" in body and "breaker" in body
        assert body["breaker"]["open"] is False

    def test_positions(self, client):
        from at60_risk.risk_manager import RiskManager

        rm = RiskManager()
        rm.positions.apply_buy("BTCUSDT", 1.0, 100.0)
        system_state.risk_manager = rm
        r = client.get("/api/positions")
        positions = r.json()["positions"]
        assert len(positions) == 1
        assert positions[0]["quantity"] == 1.0

    def test_breaker_reset_with_manager(self, client, admin_headers):
        from at60_risk.risk_manager import RiskManager

        rm = RiskManager()
        rm.breaker.manual_trip("测试")
        system_state.risk_manager = rm
        assert rm.breaker.is_open
        r = client.post("/api/breaker/reset", headers=admin_headers)
        assert r.json() == {"ok": True}
        assert not rm.breaker.is_open

    def test_market_snapshot(self, client):
        from at20_market.market_engine import MarketDataEngine

        me = MarketDataEngine(symbols=["BTCUSDT"])
        me.state["BTCUSDT"].last_price = 79979.23
        system_state.market_engine = me
        r = client.get("/api/market")
        assert r.status_code == 200
        assert r.json()["symbols"]["BTCUSDT"]["last_price"] == pytest.approx(79979.23)


class TestOperatorStatus:
    """V12.2 Dashboard 易用性: 只读聚合接口 + 部署检查页。"""

    def test_operator_status_before_engines_ready(self, client):
        """引擎未注册(系统启动中)时也必须返回可读 JSON, 不抛 500。"""
        for attr in ("risk_manager", "execution_engine", "strategy_engine", "market_engine",
                     "regime_engine", "trading_gate", "metrics", "lifecycle"):
            setattr(system_state, attr, None)
        r = client.get("/api/operator-status")
        assert r.status_code == 200
        body = r.json()
        assert body["mode"] in ("paper_testnet", "live_testnet", "paper_mainnet", "live_mainnet")
        assert body["summary"]
        assert body["next_action"]
        # 闸门未就绪必须 fail-closed
        assert body["can_buy"] is False
        assert body["can_sell"] is False

    def test_operator_status_shape(self, client):
        r = client.get("/api/operator-status")
        body = r.json()
        for key in ("mode", "mode_label", "risk_level", "status", "can_buy", "can_sell",
                    "summary", "next_action", "write_actions_enabled", "dangerous_actions",
                    "switches", "deploy", "runtime"):
            assert key in body, key

    def test_operator_status_does_not_leak_token(self, client, monkeypatch):
        from at01_common.settings import get_settings
        monkeypatch.setenv("WEB_ADMIN_TOKEN", "leak-canary-98765")
        get_settings.cache_clear()
        try:
            r = client.get("/api/operator-status")
            assert r.status_code == 200
            assert "leak-canary-98765" not in r.text
            body = r.json()
            sw = {s["key"]: s for s in body["switches"]}
            assert sw["WEB_ADMIN_TOKEN"]["value"] == "已配置"
        finally:
            get_settings.cache_clear()

    def test_ops_page_is_read_only(self, client):
        r = client.get("/ops")
        assert r.status_code == 200
        assert "部署检查" in r.text
        # 页面不得包含任何写接口调用
        for path in ("/api/emergency/kill", "/api/emergency/recover",
                     "/api/breaker/reset", "/api/shutdown"):
            assert path not in r.text

    def test_dashboard_html_has_conclusion_region(self, client):
        r = client.get("/")
        assert r.status_code == 200
        for marker in ("c-summary", "c-buy", "c-sell", "switches", "STATUS_DICT", "modal"):
            assert marker in r.text, marker
