"""V10: 急停/恢复 REST 端点集成测试

- POST /api/emergency/kill: 冻结 + 撤单(纸面撤本地 NEW 单) + 持久化 + 记事件。
- POST /api/emergency/recover: 解除 + 持久化 + 记事件。
(持久化落库已在 tests/unit/test_v10_killswitch.py 覆盖, 此处聚焦端点行为。)
"""

import pytest
from fastapi.testclient import TestClient

from at90_web import app, system_state
from at50_risk.risk_manager import RiskManager


@pytest.fixture
def client():
    """干净 TestClient, 保存/恢复全局状态"""
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


@pytest.fixture
def admin_headers(monkeypatch):
    """V11.5 P0-1: 配置写接口令牌并返回鉴权头, 结束后清缓存恢复。"""
    from at01_common.settings import get_settings

    monkeypatch.setenv("WEB_ADMIN_TOKEN", "test-token-123")
    get_settings.cache_clear()
    yield {"X-Admin-Token": "test-token-123"}
    get_settings.cache_clear()


class TestEmergencyEndpoints:
    def test_kill_not_running(self, client, admin_headers):
        system_state.risk_manager = None
        system_state.execution_engine = None
        r = client.post("/api/emergency/kill", headers=admin_headers)
        assert r.status_code == 200
        assert r.json() == {"ok": False, "msg": "not running"}

    def test_recover_not_running(self, client, admin_headers):
        system_state.risk_manager = None
        system_state.execution_engine = None
        r = client.post("/api/emergency/recover", headers=admin_headers)
        assert r.status_code == 200
        assert r.json() == {"ok": False, "msg": "not running"}

    def test_kill_arms_switch(self, client, admin_headers):
        rm = RiskManager()
        system_state.risk_manager = rm
        system_state.execution_engine = None
        r = client.post("/api/emergency/kill", headers=admin_headers)
        body = r.json()
        assert body["ok"] is True
        assert body["armed"] is True
        assert body["canceled"] == 0
        assert rm.kill_switch.is_armed
        assert rm.kill_switch.reason == "人工急停"

    def test_recover_disarms_switch(self, client, admin_headers):
        rm = RiskManager()
        rm.kill_switch.arm("急停")
        system_state.risk_manager = rm
        system_state.execution_engine = None
        r = client.post("/api/emergency/recover", headers=admin_headers)
        body = r.json()
        assert body["ok"] is True
        assert body["armed"] is False
        assert not rm.kill_switch.is_armed

    def test_kill_cancels_open_orders(self, client, db_tables, admin_headers):
        """纸面模式: 急停撤销本地 NEW 单"""
        from at60_execution.execution_executor import ExecutionEngine
        from at60_execution.execution_paper_broker import PaperOrder

        rm = RiskManager()
        engine = ExecutionEngine(risk_manager=rm)  # conftest 强制 PAPER_TRADING=true
        engine.paper.orders["oid-1"] = PaperOrder(
            client_order_id="oid-1", symbol="SOLUSDT", side="BUY",
            order_type="LIMIT", price=100.0, quantity=1.0, status="NEW",
        )
        system_state.risk_manager = rm
        system_state.execution_engine = engine

        r = client.post("/api/emergency/kill", headers=admin_headers)
        body = r.json()
        assert body["ok"] is True
        assert body["canceled"] >= 1
        assert engine.paper.orders["oid-1"].status == "CANCELED"
