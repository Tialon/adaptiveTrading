"""V13 P0 操作员事件流测试。

覆盖任务书对「日志产品化」的三条要求:
1. 用户看到的是**发生了什么**(人话), 开发者看的是**为什么发生**(技术日志仍在);
2. 「今天发生了什么」重启后仍可读(内存环 + 落库双写);
3. 事件流**不得**成为密钥泄露通道 —— 它要进日报与 AI Review。

另锚定两条安全性契约:
- `emit()` 永不抛异常(事件流是旁挂设施, 故障不得影响交易主链路);
- 急停来源 `origin` 默认 MANUAL 且持久化往返一致(fail-closed)。
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from at01_common.operator_events import (
    ALL_KINDS,
    KIND_FILL,
    KIND_KILL,
    KIND_RECONNECT,
    KIND_STARTUP,
    OperatorEventLog,
    scrub_detail,
    scrub_text,
)
from at01_common.operator_narrative import KILL_ORIGIN_AUTO_TASK, KILL_ORIGIN_MANUAL
from at50_risk.risk_killswitch import KillSwitch


# ---------------------------------------------------------------------------
# 脱敏(硬约束)
# ---------------------------------------------------------------------------


def test_scrub_detail_masks_sensitive_keys() -> None:
    out = scrub_detail({
        "binance_api_key": "AKIA-super-secret",
        "BINANCE_API_SECRET": "shh",
        "web_admin_token": "t0ken",
        "db_password": "pw",
        "symbol": "SOLUSDT",
    })
    assert out["binance_api_key"] == "<masked>"
    assert out["BINANCE_API_SECRET"] == "<masked>"
    assert out["web_admin_token"] == "<masked>"
    assert out["db_password"] == "<masked>"
    assert out["symbol"] == "SOLUSDT"  # 非敏感项原样保留


def test_scrub_detail_recurses_into_nested_structures() -> None:
    out = scrub_detail({"cfg": {"api_secret": "x"}, "items": [{"token": "y"}, "ok"]})
    assert out["cfg"]["api_secret"] == "<masked>"
    assert out["items"][0]["token"] == "<masked>"
    assert out["items"][1] == "ok"


def test_scrub_text_catches_secrets_embedded_in_free_text() -> None:
    """密钥常被拼进自由文本(下单 reason / 报错信息) —— 只按键名过滤会漏。"""
    text = "连接失败 url=https://api.binance.com?api_key=ABCDEF123456&x=1"
    assert "ABCDEF123456" not in scrub_text(text)
    assert "<masked>" in scrub_text(text)

    assert "s3cr3t" not in scrub_text("BINANCE_API_SECRET=s3cr3t")
    assert "t0ken" not in scrub_text("X-Admin-Token: t0ken")


def test_emit_scrubs_both_text_and_detail() -> None:
    log = OperatorEventLog()
    event = log.emit(
        KIND_STARTUP, "启动, key=BINANCE_API_KEY=leakme",
        detail={"api_secret": "leakme2", "symbol": "SOLUSDT"},
    )
    assert "leakme" not in event["text"]
    assert event["detail"]["api_secret"] == "<masked>"
    assert event["detail"]["symbol"] == "SOLUSDT"


# ---------------------------------------------------------------------------
# 内存环
# ---------------------------------------------------------------------------


def test_recent_returns_newest_first() -> None:
    log = OperatorEventLog()
    for i in range(5):
        log.emit(KIND_STARTUP, f"事件{i}", ts=1000 + i)
    recent = log.recent(3)
    assert [e["text"] for e in recent] == ["事件4", "事件3", "事件2"]


def test_ring_is_bounded() -> None:
    log = OperatorEventLog(capacity=3)
    for i in range(10):
        log.emit(KIND_STARTUP, f"事件{i}")
    assert len(log.recent(100)) == 3
    assert log.recent(1)[0]["text"] == "事件9"


def test_recent_filters_by_kind() -> None:
    log = OperatorEventLog()
    log.emit(KIND_STARTUP, "启动")
    log.emit(KIND_FILL, "成交")
    log.emit(KIND_FILL, "成交2")
    assert [e["text"] for e in log.recent(10, kind=KIND_FILL)] == ["成交2", "成交"]


def test_emit_without_event_loop_does_not_raise() -> None:
    """同步回调(如 RuntimeSupervisor critical-failure)里调用必须安全。"""
    log = OperatorEventLog()
    event = log.emit(KIND_KILL, "关键任务崩溃")
    assert event["kind"] == KIND_KILL
    assert log.status()["buffered"] == 1


def test_event_shape_is_complete() -> None:
    log = OperatorEventLog()
    event = log.emit(
        KIND_FILL, "成交", level="NOTICE", symbol="SOLUSDT",
        ref_type="order", ref_id="abc", detail={"qty": 1.0},
    )
    for key in ("ts", "kind", "level", "text", "symbol", "ref_type", "ref_id", "detail"):
        assert key in event
    assert isinstance(event["ts"], int)


# ---------------------------------------------------------------------------
# 落库 + 重启后仍可读
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emit_persists_and_survives_ring_clear(db_tables) -> None:
    """清掉内存环后仍能从库里读回 —— 这就是「重启后还能看到今天发生了什么」。"""
    log = OperatorEventLog()
    log.emit(KIND_STARTUP, "系统启动", detail={"version": "V13.0"})
    log.emit(KIND_RECONNECT, "行情连接已恢复")
    await log.flush()

    log.clear()
    assert log.recent(10) == []  # 内存环空了

    loaded = await log.load_recent(10)
    texts = [e["text"] for e in loaded]
    assert "系统启动" in texts
    assert "行情连接已恢复" in texts


@pytest.mark.asyncio
async def test_day_summary_counts_stability_events(db_tables) -> None:
    """「今日系统复盘」的稳定性计数来源: WS 重连 / 自动恢复 / 急停 / 人工干预。"""
    from at01_common.operator_events import KIND_RECOVER, KIND_RECOVERY, KIND_TRADE_DONE

    log = OperatorEventLog()
    now_ms = int(datetime.now().timestamp() * 1000)
    log.emit(KIND_RECONNECT, "WS 重连", ts=now_ms)
    log.emit(KIND_RECONNECT, "WS 重连", ts=now_ms)
    log.emit(KIND_RECOVERY, "自动恢复", ts=now_ms, detail={"actor": "auto"})
    log.emit(KIND_TRADE_DONE, "交易完成", ts=now_ms)
    log.emit(KIND_KILL, "人工急停", ts=now_ms, detail={"actor": "human"})
    log.emit(KIND_RECOVER, "人工恢复", ts=now_ms, detail={"actor": "human"})
    await log.flush()

    summary = await log.day_summary()
    assert summary["total"] == 6
    assert summary["ws_reconnects"] == 2
    assert summary["auto_recoveries"] == 1
    assert summary["trades"] == 1
    assert summary["kills"] == 1
    assert summary["human_interventions"] == 2  # 人工急停 + 人工恢复


@pytest.mark.asyncio
async def test_day_summary_excludes_other_days(db_tables) -> None:
    """昨天的重连不该算进今天 —— 否则「今日系统复盘」会虚报。"""
    log = OperatorEventLog()
    today_ms = int(datetime.now().timestamp() * 1000)
    log.emit(KIND_RECONNECT, "今天", ts=today_ms)
    log.emit(KIND_RECONNECT, "很久以前", ts=today_ms - 10 * 86400 * 1000)
    await log.flush()

    assert (await log.day_summary())["ws_reconnects"] == 1


@pytest.mark.asyncio
async def test_day_summary_defaults_human_when_actor_missing(db_tables) -> None:
    """actor 缺失按「人工」算 —— fail-safe: 宁可多报人工干预, 不可漏报。"""
    log = OperatorEventLog()
    log.emit(KIND_KILL, "来源不明", ts=int(datetime.now().timestamp() * 1000))
    await log.flush()
    assert (await log.day_summary())["human_interventions"] == 1


@pytest.mark.asyncio
async def test_persist_failure_never_propagates(db_tables, monkeypatch) -> None:
    """落库失败只记账不抛出 —— 事件流故障不得影响交易。"""
    log = OperatorEventLog()

    import at01_common.database as db

    class Boom:
        def __call__(self, *a, **k):
            raise RuntimeError("db down")

    monkeypatch.setattr(db, "AsyncSessionLocal", Boom())
    log.emit(KIND_STARTUP, "启动")
    await log.flush()
    assert log.status()["persist_failures"] >= 1
    assert log.recent(1)[0]["text"] == "启动"  # 内存环仍然有

    await asyncio.sleep(0)  # 让 done_callback 落地


def test_status_shape() -> None:
    log = OperatorEventLog(capacity=7)
    st = log.status()
    assert st["capacity"] == 7
    assert st["buffered"] == 0
    assert st["kinds"] == len(ALL_KINDS)


def test_all_kinds_are_declared_and_unique() -> None:
    assert len(ALL_KINDS) == len(set(ALL_KINDS))
    assert KIND_STARTUP in ALL_KINDS and KIND_FILL in ALL_KINDS


# ---------------------------------------------------------------------------
# 急停来源(origin)
# ---------------------------------------------------------------------------


def test_kill_switch_origin_defaults_to_manual() -> None:
    """不传来源 = 需要人(fail-closed)。这是整个自动恢复安全性的基石。"""
    ks = KillSwitch()
    ks.arm("某种原因")
    assert ks.origin == KILL_ORIGIN_MANUAL
    assert ks.status()["origin"] == KILL_ORIGIN_MANUAL


def test_kill_switch_records_explicit_origin() -> None:
    ks = KillSwitch()
    ks.arm("关键任务崩溃", origin=KILL_ORIGIN_AUTO_TASK)
    assert ks.origin == KILL_ORIGIN_AUTO_TASK


def test_kill_switch_origin_resets_on_disarm() -> None:
    """解除后来源归零 —— 否则下一次人工急停会继承上一次的自动来源, 变成可自愈。"""
    ks = KillSwitch()
    ks.arm("关键任务崩溃", origin=KILL_ORIGIN_AUTO_TASK)
    ks.disarm()
    ks.arm("人工急停")
    assert ks.origin == KILL_ORIGIN_MANUAL


@pytest.mark.asyncio
async def test_kill_switch_origin_persists_round_trip(db_tables) -> None:
    ks = KillSwitch()
    ks.arm("关键任务崩溃", origin=KILL_ORIGIN_AUTO_TASK)
    assert await ks.persist() is True

    reloaded = KillSwitch()
    await reloaded.load_from_db()
    assert reloaded.is_armed is True
    assert reloaded.origin == KILL_ORIGIN_AUTO_TASK
    assert reloaded.reason == "关键任务崩溃"
