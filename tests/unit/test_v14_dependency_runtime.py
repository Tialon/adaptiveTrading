"""V14 §9 依赖**运行期**行为测试(不只是启动期)。

任务书对 Redis 的要求是一条完整闭环:

    正常启动 → Redis 正常 → 应用正常
    Redis 停止 → 应用进入**明确异常状态** → 不能继续假装正常交易
    Redis 恢复 → 系统**自动恢复/重新连接** → 健康状态恢复

**实测踩到过的缺口**: `redis_status` 原本只在启动时判定一次。Redis 在运行中挂掉时
应用完全无感, 健康报告会一直显示「正常」—— 那正是任务书禁止的「继续假装正常」。
修法是 `_redis_watch_loop` 周期探测 + 自动接回。

MySQL 侧同样修了一个实测问题: 库不可达时首屏接口会**挂 12~20 秒**(建连等操作系统级
TCP 超时)。依赖不可用必须**快速失败并如实说话**, 不能把「等待」当成回答。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------------
# Redis 运行期观察(掉线发现 + 自动接回)
# ---------------------------------------------------------------------------


class _SilentLogger:
    """吞掉日志的桩(测试只关心状态机, 不关心日志输出)。"""

    def info(self, *a, **k): ...
    def warning(self, *a, **k): ...
    def exception(self, *a, **k): ...
    def error(self, *a, **k): ...


class _FakeRedis:
    """极简 redis 桩: 只为验证状态机, 不模拟协议。"""

    def __init__(self, *, alive: bool = True) -> None:
        self.alive = alive
        self.closed = False

    async def ping(self) -> bool:
        if not self.alive:
            raise ConnectionError("redis is down")
        return True

    async def aclose(self) -> None:
        self.closed = True


def _engine_with_redis(*, connected: bool) -> object:
    from at10_market.market_engine import MarketDataEngine

    eng = object.__new__(MarketDataEngine)
    eng.settings = SimpleNamespace(redis_enabled=True, redis_url="redis://x:6379/0")
    # `logger` 是 LoggerMixin 的只读 property; 塞一个静默桩到它的后备字段即可
    eng._logger = _SilentLogger()
    eng._redis = _FakeRedis(alive=connected)
    eng.bus = object() if connected else None
    eng.redis_status = {"enabled": True, "connected": connected,
                        "degraded": not connected, "error": ""}
    return eng


def test_redis_watch_marks_degraded_when_the_connection_dies() -> None:
    """运行中掉线必须被发现 —— 否则健康报告会一直骗用户说「正常」。"""
    eng = _engine_with_redis(connected=True)
    eng._redis.alive = False
    asyncio.run(eng._refresh_redis())

    assert eng.redis_status["connected"] is False
    assert eng.redis_status["degraded"] is True
    assert eng.redis_status["error"]
    assert eng.bus is None, "总线引用必须清掉, 否则发布时会持续炸"


def test_redis_watch_reconnects_automatically(monkeypatch) -> None:
    """Redis 回来后要**自动接回**, 不需要人工重启 —— 这才是「无人值守」。"""
    eng = _engine_with_redis(connected=False)

    async def _fake_connect():
        return _FakeRedis(alive=True)

    monkeypatch.setattr(eng, "_connect_redis", _fake_connect)
    monkeypatch.setattr("at20_analytics.bus.EventBus", lambda r: object())
    asyncio.run(eng._refresh_redis())

    assert eng.redis_status["connected"] is True
    assert eng.redis_status["degraded"] is False
    assert eng.bus is not None, "接回后要重建事件总线"


def test_redis_watch_skips_work_when_feature_is_off() -> None:
    """未启用 Redis 时不该有任何探测 —— Level 1 就靠这个保持零依赖。"""
    eng = _engine_with_redis(connected=False)
    eng.redis_status["enabled"] = False
    asyncio.run(eng._refresh_redis())
    assert eng.redis_status["connected"] is False


def test_redis_watch_reports_degradation_into_the_event_stream() -> None:
    """降级要进操作员事件流 —— 用户不读日志, 但会看「今天发生了什么」。"""
    from at01_common.operator_events import operator_log

    operator_log.clear()
    try:
        eng = _engine_with_redis(connected=True)
        eng._redis.alive = False
        asyncio.run(eng._refresh_redis())
        texts = [e["text"] for e in operator_log.recent(10)]
        assert any("Redis" in t and "降级" in t for t in texts), texts
    finally:
        operator_log.clear()


# ---------------------------------------------------------------------------
# MySQL: 不可达必须快速失败
# ---------------------------------------------------------------------------


def test_db_probe_has_a_timeout() -> None:
    """依赖探测必须带超时 —— 实测无超时时首屏会挂 12~20 秒。"""
    from at90_web import web_status_collect as mod

    assert mod.DB_PROBE_TIMEOUT > 0
    assert mod.DB_PROBE_TIMEOUT <= 5, "首屏等待超过 5 秒用户会以为页面卡死"


def test_mysql_connect_has_an_explicit_timeout() -> None:
    """MySQL 建连要显式超时, 而不是等操作系统级 TCP 超时。

    这条在 `database.py` 里 —— 它管的是**所有**建连, 不只是首屏探测。
    """
    src = (REPO_ROOT / "at01_common" / "database.py").read_text(encoding="utf-8")
    assert "connect_timeout" in src, "MySQL 建连缺少超时, 库不可达时全链路都会卡住"


def test_today_stats_can_skip_db_reads() -> None:
    """库已知不可达时跳过逐项读取 —— 否则 4 次超时叠加, 首屏慢成四倍。"""
    import inspect

    from at90_web.web_status_collect import collect_today_stats

    sig = inspect.signature(collect_today_stats)
    assert "skip_db" in sig.parameters


@pytest.mark.asyncio
async def test_skipping_db_reads_is_fast_and_returns_a_complete_shape() -> None:
    """跳过库读取时仍要给出**结构完整**的今日统计(页面不必写特例)。"""
    from at90_web.web_status_collect import collect_today_stats

    stats = await collect_today_stats(None, skip_db=True)
    assert stats.get("trades", 0) == 0
    assert "max_drawdown_pct" in stats
