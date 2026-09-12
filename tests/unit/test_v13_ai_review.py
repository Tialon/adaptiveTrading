"""V13 P0/P1 AI Review Package 测试。

任务书要求这个包能被 AI 直接读, 并回答一组固定问题(今天赚了还是亏了 / 哪些信号有效 /
哪个环境表现差 / 执行有没有问题 / 是否过度交易 / 与 HODL 比怎么样 / 哪些参数值得研究)。

本文件钉死三件事:

1. **文件集齐全** —— 少一个文件, 下游按名取就会 KeyError;
2. **脱敏是硬约束** —— 包会被导出、被喂给 AI、被贴进对话, 密钥绝不能在里面;
3. **不知道就说不知道** —— 没有 HODL 基准就写「尚未建立」, 不用别的数字顶上。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any

import pytest

from at70_journal.ai_review import (
    MARKDOWN_FILE,
    PACKAGE_FILES,
    AIReviewBuilder,
    _in_window,
)

TODAY = date(2026, 9, 12)


def _epoch(day: date, hour: int = 12) -> float:
    return datetime(day.year, day.month, day.day, hour).timestamp()


@pytest.fixture
def builder() -> AIReviewBuilder:
    return AIReviewBuilder(symbol="SOLUSDT")


async def _seed_trades(rows: list[dict[str, Any]]) -> None:
    from at01_common.database import AsyncSessionLocal
    from at01_common.models import ClosedTrade

    async with AsyncSessionLocal() as session:
        for r in rows:
            session.add(ClosedTrade(**r))
        await session.commit()


def _trade(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = dict(
        symbol="SOLUSDT", strategy="trend_swing", bucket="trade",
        entry_ts=_epoch(TODAY, 9), exit_ts=_epoch(TODAY, 11),
        entry_price=100.0, exit_price=102.0, quantity=1.0,
        realized_pnl=2.0, holding_seconds=7200.0,
        max_profit=2.5, max_drawdown=-1.0, regime="TRENDING", mistake_reason="",
    )
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# 文件集
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_package_writes_every_declared_file(db_tables, tmp_path) -> None:
    b = AIReviewBuilder(symbol="SOLUSDT", report_root=tmp_path)
    result = await b.write_package(TODAY)
    assert set(result["files"]) == set(PACKAGE_FILES) | {MARKDOWN_FILE}
    target = tmp_path / TODAY.isoformat()
    for name in result["files"]:
        assert (target / name).exists(), name


@pytest.mark.asyncio
async def test_package_json_files_are_valid_json(db_tables, tmp_path) -> None:
    b = AIReviewBuilder(symbol="SOLUSDT", report_root=tmp_path)
    await b.write_package(TODAY)
    target = tmp_path / TODAY.isoformat()
    for name in PACKAGE_FILES:
        json.loads((target / name).read_text(encoding="utf-8"))  # 解析失败即抛


@pytest.mark.asyncio
async def test_package_has_the_documented_top_level_shape(db_tables, tmp_path) -> None:
    b = AIReviewBuilder(symbol="SOLUSDT", report_root=tmp_path)
    package = await b.build(TODAY)
    for key in ("period", "system_status", "performance", "trades", "signals",
                "risk_events", "execution_events", "market_regimes", "anomalies",
                "strategy_version", "recommendation_candidates"):
        assert key in package, key


# ---------------------------------------------------------------------------
# 脱敏(硬约束)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_secrets_never_reach_the_package(db_tables, tmp_path) -> None:
    """密钥可能被拼进自由文本(下单 reason / 报错 detail) —— 必须按值扫描, 不只按键名。"""
    from at01_common.database import AsyncSessionLocal
    from at01_common.models import RiskEvent, Signal

    async with AsyncSessionLocal() as session:
        session.add(Signal(
            symbol="SOLUSDT", strategy="trend_swing", side="BUY", price=100.0,
            reason="连接失败 url=https://api.binance.com?api_key=LEAKEDKEY123",
            created_at=datetime(TODAY.year, TODAY.month, TODAY.day, 10),
        ))
        session.add(RiskEvent(
            event_type="reject", symbol="SOLUSDT",
            detail="BINANCE_API_SECRET=LEAKEDSECRET456",
            created_at=datetime(TODAY.year, TODAY.month, TODAY.day, 11),
        ))
        await session.commit()

    b = AIReviewBuilder(symbol="SOLUSDT", report_root=tmp_path)
    result = await b.write_package(TODAY)
    blob = json.dumps(result["package"], ensure_ascii=False)
    assert "LEAKEDKEY123" not in blob
    assert "LEAKEDSECRET456" not in blob
    # 页面/文件里也一样
    assert "LEAKEDKEY123" not in (tmp_path / TODAY.isoformat() / MARKDOWN_FILE).read_text(
        encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_sensitive_keys_in_payload_are_masked(db_tables, tmp_path) -> None:
    from at01_common.database import AsyncSessionLocal
    from at01_common.models import ExecutionEvent

    async with AsyncSessionLocal() as session:
        session.add(ExecutionEvent(
            event_id="e1", client_order_id="c1", event_type="ACK",
            event_time=int(_epoch(TODAY) * 1000),
            payload=json.dumps({"api_key": "SHOULD-NOT-APPEAR", "qty": 1}),
        ))
        await session.commit()

    b = AIReviewBuilder(symbol="SOLUSDT", report_root=tmp_path)
    package = await b.build(TODAY)
    assert "SHOULD-NOT-APPEAR" not in json.dumps(package, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 时间窗口
# ---------------------------------------------------------------------------


def test_window_accepts_seconds_and_normalises_milliseconds() -> None:
    start = _epoch(TODAY, 0)
    end = start + 86400
    assert _in_window(_epoch(TODAY, 12), start, end) is True
    assert _in_window(_epoch(TODAY, 12) * 1000, start, end) is True  # 毫秒
    assert _in_window(_epoch(TODAY) - 10, start - 100, start) is False


def test_window_handles_datetime_and_garbage() -> None:
    start = _epoch(TODAY, 0)
    end = start + 86400
    assert _in_window(datetime(TODAY.year, TODAY.month, TODAY.day, 8), start, end) is True
    assert _in_window(None, start, end) is False
    assert _in_window("not-a-time", start, end) is False


@pytest.mark.asyncio
async def test_only_todays_trades_are_included(db_tables, tmp_path) -> None:
    """昨天的成交不该混进今天的复盘 —— 那会让「今日盈亏」虚高。"""
    await _seed_trades([
        _trade(),
        _trade(exit_ts=_epoch(TODAY - timedelta(days=1), 11), realized_pnl=99.0),
    ])
    b = AIReviewBuilder(symbol="SOLUSDT", report_root=tmp_path)
    package = await b.build(TODAY)
    assert len(package["trades"]) == 1
    assert package["performance"]["total_pnl"] == 2.0


# ---------------------------------------------------------------------------
# 绩效与派生
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_performance_math(db_tables, tmp_path) -> None:
    await _seed_trades([
        _trade(realized_pnl=3.0),
        _trade(realized_pnl=-1.0),
        _trade(realized_pnl=2.0),
    ])
    b = AIReviewBuilder(symbol="SOLUSDT", report_root=tmp_path)
    perf = (await b.build(TODAY))["performance"]
    assert perf["trades"] == 3
    assert perf["wins"] == 2 and perf["losses"] == 1
    assert perf["total_pnl"] == 4.0
    # 比率按 4 位小数落盘(JSON 友好), 不是全精度 —— 断言按这个口径
    assert abs(perf["win_rate"] - 2 / 3) < 1e-3
    assert perf["profit_factor"] == 5.0
    assert perf["best_trade"] == 3.0 and perf["worst_trade"] == -1.0


def test_profit_factor_is_none_when_there_are_no_losses() -> None:
    """没有亏损时盈亏比是「不适用」, 不是无穷大也不是 0。"""
    import asyncio

    b = AIReviewBuilder()
    perf = asyncio.run(b._collect_performance([{"realized_pnl": 1.0, "holding_seconds": 1.0}]))
    assert perf["profit_factor"] is None


@pytest.mark.asyncio
async def test_market_regimes_sorted_worst_first(db_tables, tmp_path) -> None:
    """任务书要回答「哪个市场环境表现差」—— 表首就该是最差的。"""
    await _seed_trades([
        _trade(regime="RANGING", realized_pnl=-5.0),
        _trade(regime="TRENDING", realized_pnl=4.0),
        _trade(regime="PANIC", realized_pnl=-9.0),
    ])
    b = AIReviewBuilder(symbol="SOLUSDT", report_root=tmp_path)
    regimes = (await b.build(TODAY))["market_regimes"]
    assert [r["regime"] for r in regimes] == ["PANIC", "RANGING", "TRENDING"]


@pytest.mark.asyncio
async def test_performance_by_strategy_is_split(db_tables, tmp_path) -> None:
    await _seed_trades([
        _trade(strategy="trend_swing", realized_pnl=5.0),
        _trade(strategy="mean_reversion", realized_pnl=-2.0),
    ])
    b = AIReviewBuilder(symbol="SOLUSDT", report_root=tmp_path)
    by = (await b.build(TODAY))["performance_by_strategy"]
    assert {s["strategy"]: s["pnl"] for s in by} == {
        "trend_swing": 5.0, "mean_reversion": -2.0,
    }


# ---------------------------------------------------------------------------
# 异常与建议候选
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rejected_signals_are_flagged_for_review(db_tables, tmp_path) -> None:
    """「风控有没有误杀」—— 未采纳的信号必须出现在问题清单里。"""
    from at01_common.database import AsyncSessionLocal
    from at01_common.models import Signal

    async with AsyncSessionLocal() as session:
        for i in range(3):
            session.add(Signal(
                symbol="SOLUSDT", strategy="decision", side="SELL", price=100.0,
                status="rejected",
                created_at=datetime(TODAY.year, TODAY.month, TODAY.day, 10, i),
            ))
        await session.commit()

    b = AIReviewBuilder(symbol="SOLUSDT", report_root=tmp_path)
    package = await b.build(TODAY)
    kinds = [a["kind"] for a in package["anomalies"]]
    assert "signals_rejected" in kinds
    assert package["recommendation_candidates"], "异常必须能转成研究候选"


@pytest.mark.asyncio
async def test_overtrading_is_flagged(db_tables, tmp_path) -> None:
    from at70_journal.ai_review import _OVERTRADING_TRADES

    await _seed_trades([_trade() for _ in range(_OVERTRADING_TRADES + 1)])
    b = AIReviewBuilder(symbol="SOLUSDT", report_root=tmp_path)
    kinds = [a["kind"] for a in (await b.build(TODAY))["anomalies"]]
    assert "possible_overtrading" in kinds


@pytest.mark.asyncio
async def test_quiet_day_reports_no_anomalies(db_tables, tmp_path) -> None:
    b = AIReviewBuilder(symbol="SOLUSDT", report_root=tmp_path)
    assert (await b.build(TODAY))["anomalies"] == []


# ---------------------------------------------------------------------------
# 人读 Markdown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_markdown_answers_the_documented_questions(db_tables, tmp_path) -> None:
    await _seed_trades([_trade(realized_pnl=-1.5)])
    b = AIReviewBuilder(symbol="SOLUSDT", report_root=tmp_path)
    md = (await b.write_package(TODAY))["package"]
    text = b.render_markdown(md)
    for section in ("## 结论", "## 策略表现", "## 市场环境", "## 系统稳定性",
                    "## 问题", "## 建议 AI 进一步研究", "## 策略版本", "## 与 HODL 对比"):
        assert section in text, section
    assert "亏了" in text


@pytest.mark.asyncio
async def test_markdown_says_no_trades_instead_of_showing_zero_win_rate(
    db_tables, tmp_path
) -> None:
    """没有成交时不能显示「胜率 0%」—— 那会被读成「全亏」。"""
    b = AIReviewBuilder(symbol="SOLUSDT", report_root=tmp_path)
    md = (await b.write_package(TODAY))["package"]
    text = b.render_markdown(md)
    assert "今天没有成交" in text


@pytest.mark.asyncio
async def test_markdown_states_missing_hodl_baseline_honestly(db_tables, tmp_path) -> None:
    """没有基准就说没有, **不用别的数字顶上** —— 这是诚实原则的底线。"""
    b = AIReviewBuilder(symbol="SOLUSDT", report_root=tmp_path)
    md = (await b.write_package(TODAY))["package"]
    text = b.render_markdown(md)
    assert "尚未建立 HODL 基准" in text
    assert "不用其它数字代替" in text


def test_markdown_declares_ai_cannot_trade() -> None:
    """红线必须写在产物里 —— 读这份文件的人(或 AI)要看得到它。"""
    b = AIReviewBuilder()
    text = b.render_markdown({"period": {"date": "2026-09-12"}, "performance": {}})
    assert "不直接下单" in text


# ---------------------------------------------------------------------------
# 目录发现
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_latest_dir_returns_the_newest_day(db_tables, tmp_path) -> None:
    b = AIReviewBuilder(symbol="SOLUSDT", report_root=tmp_path)
    await b.write_package(date(2026, 9, 10))
    await b.write_package(date(2026, 9, 12))
    assert b.latest_dir().name == "2026-09-12"


def test_latest_dir_is_none_when_nothing_generated(tmp_path) -> None:
    assert AIReviewBuilder(report_root=tmp_path / "nope").latest_dir() is None
