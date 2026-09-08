"""V11.5 P0-1 Web Security Hardening 测试。

覆盖:
1. 默认 API_HOST = 127.0.0.1(settings / start_server / standalone 三处)。
2. 写接口未配置令牌 → 锁定(503 fail-closed)。
3. 写接口令牌错误 → 401 未授权。
4. 写接口令牌正确 → 放行(合法授权路径成功)。
5. 所有写接口(breaker reset / emergency kill / emergency recover / shutdown)安全策略一致。
6. GET 查询接口无鉴权、不受影响。
7. 非回环 API_HOST 未配令牌 → validate() fail-fast。
"""

import inspect
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from at01_common.settings import Settings, get_settings
from at10_web import app, system_state

# 全部写接口路径(同一鉴权策略)
WRITE_ENDPOINTS = [
    "/api/breaker/reset",
    "/api/emergency/kill",
    "/api/emergency/recover",
    "/api/shutdown",
]


@pytest.fixture
def client():
    saved = {
        "running": system_state.running,
        "risk_manager": system_state.risk_manager,
        "execution_engine": system_state.execution_engine,
    }
    system_state.running = True
    yield TestClient(app)
    system_state.running = saved["running"]
    system_state.risk_manager = saved["risk_manager"]
    system_state.execution_engine = saved["execution_engine"]


def _set_token(monkeypatch, token):
    if token is None:
        monkeypatch.delenv("WEB_ADMIN_TOKEN", raising=False)
    else:
        monkeypatch.setenv("WEB_ADMIN_TOKEN", token)
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# 1. 默认 host 安全
# ---------------------------------------------------------------------------

class TestDefaultHostSecurity:
    def test_settings_default_host_loopback(self):
        assert Settings.model_fields["api_host"].default == "127.0.0.1"

    def test_start_server_default_host_loopback(self):
        from at10_web.web_app import start_server

        assert inspect.signature(start_server).parameters["host"].default == "127.0.0.1"

    def test_standalone_default_host_loopback(self):
        src = (Path(__file__).parents[2] / "at10_web" / "web_serve_standalone.py").read_text(
            encoding="utf-8"
        )
        assert '"--host", default="127.0.0.1"' in src


# ---------------------------------------------------------------------------
# 2/3/4/5. 写接口鉴权策略
# ---------------------------------------------------------------------------

class TestWriteEndpointAuth:
    def test_locked_when_no_token(self, client, monkeypatch):
        """未配置 WEB_ADMIN_TOKEN → 所有写接口 503(fail-closed)。"""
        _set_token(monkeypatch, None)
        for path in WRITE_ENDPOINTS:
            r = client.post(path)
            assert r.status_code == 503, f"{path} 应锁定"

    def test_unauthorized_wrong_token(self, client, monkeypatch):
        """令牌错误 → 401 未授权。"""
        _set_token(monkeypatch, "correct-token")
        for path in WRITE_ENDPOINTS:
            r = client.post(path, headers={"X-Admin-Token": "wrong"})
            assert r.status_code == 401, f"{path} 应拒绝错误令牌"

    def test_authorized_breaker_reset(self, client, monkeypatch):
        """合法令牌 → 放行, breaker reset 实际生效。"""
        from at60_risk.risk_manager import RiskManager

        _set_token(monkeypatch, "correct-token")
        rm = RiskManager()
        rm.breaker.manual_trip("测试")
        system_state.risk_manager = rm
        assert rm.breaker.is_open

        r = client.post("/api/breaker/reset", headers={"X-Admin-Token": "correct-token"})
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        assert not rm.breaker.is_open

    def test_authorized_shutdown(self, client, monkeypatch):
        """合法令牌 → shutdown 写接口放行。"""
        _set_token(monkeypatch, "correct-token")
        system_state.extra["shutdown_requested"] = False
        r = client.post("/api/shutdown", headers={"X-Admin-Token": "correct-token"})
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        assert system_state.extra["shutdown_requested"] is True


# ---------------------------------------------------------------------------
# 6. GET 查询接口不受影响
# ---------------------------------------------------------------------------

class TestGetEndpointsUnaffected:
    def test_get_health_no_auth(self, client, monkeypatch):
        _set_token(monkeypatch, None)
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_get_system_no_auth(self, client, monkeypatch):
        _set_token(monkeypatch, None)
        r = client.get("/api/system")
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# 7. 非回环 host 未配令牌 → fail-fast
# ---------------------------------------------------------------------------

class TestValidateNonLoopback:
    def test_nonloopback_without_token_blocked(self):
        cfg = Settings(_env_file=None, api_host="0.0.0.0", web_admin_token="")
        problems = cfg.validate()
        assert any("WEB_ADMIN_TOKEN" in p for p in problems)

    def test_nonloopback_with_token_passes_this_check(self):
        cfg = Settings(_env_file=None, api_host="0.0.0.0", web_admin_token="secret")
        problems = cfg.validate()
        assert not any("WEB_ADMIN_TOKEN" in p for p in problems)

    def test_loopback_default_no_problem(self):
        cfg = Settings(_env_file=None)
        assert not any("WEB_ADMIN_TOKEN" in p for p in cfg.validate())
