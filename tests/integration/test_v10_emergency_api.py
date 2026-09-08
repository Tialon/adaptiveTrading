"""V10: 急停/恢复 REST 端点集成测试

- POST /api/emergency/kill: 冻结 + 撤单(纸面撤本地 NEW 单) + 持久化 + 记事件。
- POST /api/emergency/recover: 解除 + 持久化 + 记事件。
(持久化落库已在 tests/unit/test_v10_killswitch.py 覆盖, 此处聚焦端点行为。)
"""

import pytest
from fastapi.testclient import TestClient

from at10_web import app, system_state
from at60_risk.risk_manager import RiskManager


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


class TestEmergencyEndpoints:
    def test_kill_not_running(self, client):
        system_state.risk_manager = None
        system_state.execution_engine = None
        r = client.post("/api/emergency/kill")
        assert r.status_code == 200
        assert r.json() == {"ok": False, "msg": "not running"}

    def test_recover_not_running(self, client):
        system_state.risk_manager = None
        system_state.execution_engine = None
        r = client.post("/api/emergency/recover")
        assert r.status_code == 200
        assert r.json() == {"ok": False, "msg": "not running"}

    def test_kill_arms_switch(self, client):
        rm = RiskManager()
        system_state.risk_manager = rm
        system_state.execution_engine = None
        r = client.post("/api/emergency/kill")
        body = r.json()
        assert body["ok"] is True
        assert body["armed"] is True
        assert body["canceled"] == 0
        assert rm.kill_switch.is_armed
        assert rm.kill_switch.reason == "人工急停"

    def test_recover_disarms_switch(self, client):
        rm = RiskManager()
        rm.kill_switch.arm("急停")
        system_state.risk_manager = rm
        system_state.execution_engine = None
        r = client.post("/api/emergency/recover")
        body = r.json()
        assert body["ok"] is True
        assert body["armed"] is False
        assert not rm.kill_switch.is_armed

    def test_kill_cancels_open_orders(self, client, db_tables):
        """纸面模式: 急停撤销本地 NEW 单"""
        from at50_execution.execution_executor import ExecutionEngine
        from at50_execution.execution_paper_broker import PaperOrder

        rm = RiskManager()
        engine = ExecutionEngine(risk_manager=rm)  # conftest 强制 PAPER_TRADING=true
        engine.paper.orders["oid-1"] = PaperOrder(
            client_order_id="oid-1", symbol="SOLUSDT", side="BUY",
            order_type="LIMIT", price=100.0, quantity=1.0, status="NEW",
        )
        system_state.risk_manager = rm
        system_state.execution_engine = engine

        r = client.post("/api/emergency/kill")
        body = r.json()
        assert body["ok"] is True
        assert body["canceled"] >= 1
        assert engine.paper.orders["oid-1"].status == "CANCELED"
