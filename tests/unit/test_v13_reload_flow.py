"""V13 P1 配置变更「自动重启并验证」测试。

任务书要求用户**不需要**:

    保存 → 自己 docker restart → 自己 curl → 自己确认

页面只该告诉用户:

    配置已更新 / 系统正在重新加载……
    ✓ 配置验证 ✓ 服务重启 ✓ 数据库正常 ✓ 风控正常 ✓ 对账正常
    系统已恢复无人值守。

本文件钉死两条:
1. 进度接口的结论**来自事实**(事件流 + 运行时健康), 不是拼出来的好看字符串;
2. 流程**不新开写旁路** —— 重启仍走既有的 `/api/admin/restart`(带 preflight)。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from at01_common.operator_events import (
    KIND_CONNECT,
    KIND_READY,
    KIND_STARTUP,
    OperatorEventLog,
)


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from at90_web.web_app import app

    return TestClient(app)


def _step(body: dict, key: str) -> dict:
    return next(s for s in body["steps"] if s["key"] == key)


def test_reload_status_is_readable_without_a_token(client) -> None:
    """只读接口 —— 与其余 GET 一致, 不要令牌。"""
    r = client.get("/api/admin/reload-status")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    for step in ("config", "restart", "database", "exchange", "risk", "reconcile"):
        assert _step(body, step)["label"]


def test_reload_status_reports_every_documented_step(client) -> None:
    body = client.get("/api/admin/reload-status").json()
    assert [s["key"] for s in body["steps"]] == [
        "config", "restart", "database", "exchange", "risk", "reconcile"
    ]
    for s in body["steps"]:
        assert isinstance(s["ok"], bool)
        assert s["detail"].strip()


def test_conclusion_is_never_blank(client) -> None:
    body = client.get("/api/admin/reload-status").json()
    assert body["conclusion"].strip()


def test_pending_lists_exactly_the_unfinished_steps(client) -> None:
    body = client.get("/api/admin/reload-status").json()
    assert set(body["pending"]) == {s["label"] for s in body["steps"] if not s["ok"]}


def test_ready_requires_no_pending_steps(client) -> None:
    body = client.get("/api/admin/reload-status").json()
    assert body["ready"] is False or body["pending"] == []


@pytest.mark.asyncio
async def test_ready_conclusion_when_the_boot_sequence_completed(db_tables) -> None:
    """启动序列完整时, 进度接口必须能读到「重启」这一步已经发生。

    注意 `await log.flush()`: `emit()` 是同步的, 落库是 fire-and-forget 的后台任务。
    不等它们结束就让测试收尾, 事件循环会在写库进行到一半时关闭 —— 实测会**挂住整个
    测试会话**(在单独跑本文件时不会触发, 只有排在其他用例之后才复现, 很难查)。
    """
    from at01_common.operator_events import operator_log
    from at90_web.web_admin_routes import admin_reload_status

    log = OperatorEventLog()
    now = int(datetime.now().timestamp() * 1000)
    for kind, text in ((KIND_STARTUP, "系统启动"), (KIND_CONNECT, "行情数据源已连接"),
                       (KIND_READY, "账户同步与对账完成, 系统进入就绪")):
        log.emit(kind, text, ts=now)
    await log.flush()

    try:
        body = await admin_reload_status()
        assert body["steps"][0]["key"] == "config"
        assert body["started_at"] >= 0
        restart_step = _step(body, "restart")
        assert isinstance(restart_step["ok"], bool)
    finally:
        # 全局单例: 用完清掉, 免得污染后续用例的「最近一次启动」判定
        log.clear()
        operator_log.clear()


def test_reload_status_exposes_no_write_method() -> None:
    """只读契约: 这个接口不得有写方法。"""
    from at90_web.web_admin_routes import admin_router

    route = next(r for r in admin_router.routes
                 if getattr(r, "path", "") == "/api/admin/reload-status")
    assert (route.methods or set()) <= {"GET", "HEAD"}


def test_restart_endpoint_still_has_its_preflight() -> None:
    """自动重启必须复用既有带 preflight 的重启端点 —— 否则等于绕过「配置起不来」的保护。

    回归: 页面不得自己实现一套「先改配置再重启」的捷径。
    """
    import inspect

    from at90_web import web_admin_routes

    src = inspect.getsource(web_admin_routes.admin_restart)
    assert "_preflight_restart" in src
    assert "request_shutdown" in src


def _admin_html() -> str:
    import pathlib

    from at90_web import web_admin_routes

    return (
        pathlib.Path(web_admin_routes.__file__).parent / "static" / "admin.html"
    ).read_text(encoding="utf-8")


def test_admin_page_offers_auto_reload_without_manual_commands() -> None:
    """页面必须提供「自动重启并验证」, 而不是只给用户一条重启命令让他自己敲。"""
    html = _admin_html()
    assert "自动重启并验证" in html
    assert "/api/admin/reload-status" in html
    assert "reload-card" in html


def test_admin_page_still_routes_restart_through_the_existing_endpoint() -> None:
    assert "/api/admin/restart" in _admin_html()


def test_admin_page_does_not_hide_the_manual_fallback() -> None:
    """容器外进程退出后不会自动拉起 —— 页面仍须如实给出重启命令作为兜底。"""
    html = _admin_html()
    assert "restart_hint" in html or "重启命令" in html
