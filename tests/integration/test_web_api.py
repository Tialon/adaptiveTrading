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
