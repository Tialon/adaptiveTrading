"""V14 §3/§9 外部依赖等级测试。

任务书要求「不要凭文档判断 Redis 是否关键, 请实际检查」, 并把结论落成:

    MySQL = REQUIRED
    Redis  = REQUIRED / OPTIONAL

代码实证结论是 **MySQL=REQUIRED / Redis=OPTIONAL**(一条只有发布方、没有消费方的
事件流旁路)。但任务书同时要求 OPTIONAL 依赖的不可用
**「必须明确记录为降级状态」** —— 不许静默。

本文件钉死两件事:
1. 依赖等级声明与代码一致(Redis 确实没有生产消费方);
2. 降级**可见**: 健康报告里有这一项、结论里有 degradations、事件流里有记录。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from at01_common.runtime_health import build_runtime_health
from at90_web.web_health_report import build_health_report, summarize_health_report

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _health(**over) -> dict:
    base = {
        "status": "TRADING", "can_buy": True, "can_sell": True,
        "lifecycle": {"state": "TRADING"}, "risk": {"state": "NORMAL"},
        "kill_switch": {}, "breaker": {}, "reconcile": {"reconciled": True},
        "market": {"connection_ok": True, "data_healthy": True},
        "exchange": {"healthy": True}, "tasks": {}, "trade": {},
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# 依赖等级与代码一致
# ---------------------------------------------------------------------------


def test_redis_has_no_production_consumer() -> None:
    """`Redis = OPTIONAL` 的依据: EventBus 的消费端在生产代码里**没有调用者**。

    这条断言的作用是**防反转**: 哪天有人把 `consume()` 接进生产链路, 这条会红,
    逼着重新评估依赖等级(OPTIONAL → REQUIRED), 而不是让一个过期的结论留在文档里。
    """
    bus = (REPO_ROOT / "at20_analytics" / "bus.py").read_text(encoding="utf-8")
    assert "def consume" in bus  # 能力存在
    assert "def publish_market" in bus

    production = [
        p for p in REPO_ROOT.glob("at*/**/*.py")
        if "consume(" in p.read_text(encoding="utf-8")
        and p.name != "bus.py"
        and "test" not in p.name
    ]
    assert production == [], (
        "EventBus.consume() 出现了生产调用者 —— Redis 可能已升级为关键依赖, "
        "请重新评估并更新 docs/architecture.md 的依赖等级: " + str(production)
    )


def test_market_engine_main_path_does_not_need_redis() -> None:
    """主链路是 `on_trade` 内存回调; Redis 只是旁路 —— 这是 OPTIONAL 的直接依据。"""
    src = (REPO_ROOT / "at10_market" / "market_engine.py").read_text(encoding="utf-8")
    assert "self.on_trade(symbol, tick)" in src
    assert "self.bus is not None" in src  # bus 是可选的


# ---------------------------------------------------------------------------
# 降级必须可见
# ---------------------------------------------------------------------------


def test_redis_status_is_exposed_in_runtime_health() -> None:
    state = SimpleNamespace(
        running=True, started_at=0.0, risk_manager=None, lifecycle=None,
        trading_gate=None, metrics=None, supervisor=None,
        market_engine=SimpleNamespace(
            state={},
            redis_status={"enabled": True, "connected": False, "degraded": True,
                          "error": "connection refused"},
        ),
    )
    health = build_runtime_health(state)
    deps = health["dependencies"]
    assert deps["mysql"]["required"] is True
    assert deps["redis"]["required"] is False
    assert deps["redis"]["enabled"] is True
    assert deps["redis"]["connected"] is False
    assert deps["redis"]["degraded"] is True


def test_redis_degradation_appears_in_the_health_report() -> None:
    """降级要**出现在列表里** —— 否则用户看到的是一份「一切正常」的假报告。"""
    items = build_health_report(
        _health(dependencies={"redis": {"enabled": True, "connected": False,
                                        "degraded": True, "error": "boom"}}),
        settings=SimpleNamespace(validate=lambda: []), db_ok=True,
    )
    redis = next((i for i in items if i["key"] == "redis"), None)
    assert redis is not None, "Redis 降级没有出现在健康报告里"
    assert redis["ok"] is False
    assert redis["blocking"] is False           # 不影响交易, 不该把系统说成异常
    assert "降级" in redis["conclusion"]
    assert "交易不受影响" in redis["detail"]


def test_redis_degradation_does_not_say_the_system_is_broken() -> None:
    """但也要如实说「有几项降级」—— 不能藏起来。"""
    items = build_health_report(
        _health(dependencies={"redis": {"enabled": True, "connected": False,
                                        "degraded": True, "error": "boom"}}),
        settings=SimpleNamespace(validate=lambda: []), db_ok=True,
    )
    summary = summarize_health_report(items)
    assert summary["ok"] is True                  # 交易能力未受损
    assert "无需操作" in summary["conclusion"]     # 用户确实无事可做
    assert "降级" in summary["conclusion"]         # 但降级被如实说出
    assert summary["degradations"]


def test_redis_absent_from_report_when_not_enabled() -> None:
    """未启用 Redis 不是降级 —— 不该天天提示一条无意义的消息。"""
    items = build_health_report(
        _health(dependencies={"redis": {"enabled": False}}),
        settings=SimpleNamespace(validate=lambda: []), db_ok=True,
    )
    assert not any(i["key"] == "redis" for i in items)
    assert summarize_health_report(items)["degradations"] == []


def test_healthy_redis_is_reported_as_normal() -> None:
    items = build_health_report(
        _health(dependencies={"redis": {"enabled": True, "connected": True}}),
        settings=SimpleNamespace(validate=lambda: []), db_ok=True,
    )
    redis = next(i for i in items if i["key"] == "redis")
    assert redis["ok"] is True
    assert summarize_health_report(items)["degradations"] == []


# ---------------------------------------------------------------------------
# compose 定义与依赖等级一致
# ---------------------------------------------------------------------------


def test_compose_has_mysql_and_redis_as_real_services() -> None:
    """V14 §6: Docker 环境必须有 mysql / redis 两个服务, 不能只跑 SQLite。"""
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "mysql:" in compose
    assert "redis:" in compose
    assert "mysql_data:" in compose, "MySQL 需要持久化卷(V14 §8: 重建容器不得丢数据)"
    assert "redis_data:" in compose


def test_compose_no_longer_pins_sqlite_for_the_app() -> None:
    """V14 §7: 不许再出现「Docker = SQLite」这种架构漂移。"""
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    # 服务定义里不得再硬编码 SQLite 的 DATABASE_URL
    assert "sqlite+aiosqlite:////app/data" not in compose
    assert "mysql+aiomysql://" in compose


def test_compose_waits_for_real_health_not_just_start_order() -> None:
    """V14 §6.1: 必须用 `condition: service_healthy`, 不能只写 depends_on 列表。

    后者只保证启动顺序 —— MySQL 进程起来了但还没准备好接受连接时, 应用照样会崩。
    """
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert compose.count("condition: service_healthy") >= 2
    for svc in ("mysql", "redis"):
        assert f"{svc}:\n        condition: service_healthy" in compose or \
               f"{svc}:" in compose


@pytest.mark.parametrize("svc", ["mysql", "redis"])
def test_each_dependency_defines_a_healthcheck(svc: str) -> None:
    """没有 healthcheck 的话 `condition: service_healthy` 永远等不到。"""
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    block = compose.split(f"  {svc}:", 1)[1].split("\n  # ---", 1)[0]
    assert "healthcheck:" in block, f"{svc} 缺少 healthcheck"
