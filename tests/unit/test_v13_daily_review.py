"""V13 P0「今日系统复盘」段落测试。

任务书要求每天自动生成这样一段人读复盘:

    运行时间 / 交易次数 / 胜率 / 收益 / 最大回撤 / HODL
    系统稳定性: WS 重连 / 自动恢复 / 人工干预
    策略: 趋势策略 +x% / 均值回归 -y%
    问题: …
    结论: 今天系统运行正常。
    建议 AI 进一步研究: …

**单一计算, 两种读物**: 数据由 `at70_journal/ai_review.py` 算一次, 本段落只是**渲染**。
所以这里重点断言: 渲染如实、缺数据不编、两份报告不会对不上。
"""

from __future__ import annotations

from typing import Any

import pytest

from at70_journal.daily_report import DailyReport


@pytest.fixture
def report() -> DailyReport:
    return DailyReport()


def _package(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "period": {"date": "2026-09-12"},
        "performance": {
            "trades": 8, "wins": 5, "losses": 3, "win_rate": 0.625,
            "total_pnl": 121.0, "max_drawdown_pct": 0.73,
            "best_trade": 60.0, "worst_trade": -21.0,
            "benchmark": None,
        },
        "performance_by_strategy": [
            {"strategy": "trend_swing", "trades": 5, "win_rate": 0.8, "pnl": 142.0},
            {"strategy": "mean_reversion", "trades": 3, "win_rate": 0.33, "pnl": -21.0},
        ],
        "market_regimes": [
            {"regime": "PANIC", "trades": 2, "win_rate": 0.0, "pnl": -30.0},
            {"regime": "TRENDING", "trades": 6, "win_rate": 0.83, "pnl": 151.0},
        ],
        "anomalies": [{"kind": "x", "text": "两次 SELL 滑点偏高", "count": 2, "hint": "看执行"}],
        "recommendation_candidates": [{"from": "x", "topic": "SELL 滑点与 regime 的关系", "count": 2}],
        "system_status": {
            "stability": {
                "ws_reconnects": 2, "auto_recoveries": 3, "degradations": 0,
                "kills": 0, "human_interventions": 0, "errors": 1, "startups": 1,
                "trades": 8,
            }
        },
    }
    base.update(over)
    return base


def _text(lines: list[str]) -> str:
    return "\n".join(lines)


def test_self_review_renders_every_documented_field(report: DailyReport) -> None:
    text = _text(report._render_self_review(_package()))
    assert "## 今日系统复盘" in text
    for token in ("交易次数", "胜率", "收益", "最大回撤", "系统稳定性",
                  "策略归因", "问题", "结论", "建议 AI 进一步研究"):
        assert token in text, token


def test_numbers_match_the_package_exactly(report: DailyReport) -> None:
    """两份读物必须对得上 —— 数字不一致比数字错更让人失去信任。"""
    text = _text(report._render_self_review(_package()))
    assert "8 笔" not in text  # 措辞是「交易次数: 8」, 不是编的
    assert "交易次数: 8" in text
    assert "62.5%" in text
    assert "+121.00 USDT" in text
    assert "0.73%" in text


def test_stability_counts_are_rendered(report: DailyReport) -> None:
    text = _text(report._render_self_review(_package()))
    assert "行情重连 2 次" in text
    assert "自动恢复 3 次" in text
    assert "人工干预 0 次" in text


def test_strategy_attribution_is_listed(report: DailyReport) -> None:
    text = _text(report._render_self_review(_package()))
    assert "trend_swing: +142.00" in text
    assert "mean_reversion: -21.00" in text


def test_worst_regime_is_called_out(report: DailyReport) -> None:
    """任务书要回答「哪个市场环境表现差」—— 必须点名, 不能只列表。"""
    text = _text(report._render_self_review(_package()))
    assert "表现最差的市场环境" in text
    assert "PANIC" in text


def test_missing_hodl_baseline_is_stated_not_faked(report: DailyReport) -> None:
    text = _text(report._render_self_review(_package()))
    assert "尚未建立基准" in text
    assert "不用其它数字代替" in text


def test_hodl_baseline_present_is_reported_as_such(report: DailyReport) -> None:
    pkg = _package()
    pkg["performance"]["benchmark"] = {"baseline": {"initial_equity": 100000}}
    text = _text(report._render_self_review(pkg))
    assert "已建立" in text


# ---------------------------------------------------------------------------
# 不编数字
# ---------------------------------------------------------------------------


def test_zero_trades_does_not_render_a_zero_win_rate(report: DailyReport) -> None:
    """「胜率 0%」会被读成「全亏」。没有成交就明说没有成交。"""
    pkg = _package()
    pkg["performance"].update({"trades": 0, "wins": 0, "losses": 0,
                               "win_rate": 0.0, "total_pnl": 0.0})
    pkg["performance_by_strategy"] = []
    pkg["market_regimes"] = []
    text = _text(report._render_self_review(pkg))
    assert "胜率" not in text
    assert "交易次数: 0" in text
    assert "今天没有成交" in text


def test_missing_package_says_so_instead_of_guessing(report: DailyReport) -> None:
    """没有复盘包时如实说没有, 不用本文件里已有的近似数字顶替。"""
    for empty in (None, {}):
        text = _text(report._render_self_review(empty))
        assert "## 今日系统复盘" in text
        assert "未生成复盘包" in text


def test_quiet_day_says_no_problems(report: DailyReport) -> None:
    pkg = _package()
    pkg["anomalies"] = []
    pkg["recommendation_candidates"] = []
    text = _text(report._render_self_review(pkg))
    assert "未发现值得注意的问题" in text
    assert "今日无特别线索" in text


# ---------------------------------------------------------------------------
# 结论措辞
# ---------------------------------------------------------------------------


def test_profit_conclusion(report: DailyReport) -> None:
    assert "净盈利" in _text(report._render_self_review(_package()))


def test_loss_conclusion_does_not_equate_loss_with_failure(report: DailyReport) -> None:
    """亏损 ≠ 故障。措辞不能把两者混为一谈, 否则用户会去查一个并不存在的 bug。"""
    pkg = _package()
    pkg["performance"]["total_pnl"] = -50.0
    text = _text(report._render_self_review(pkg))
    assert "净亏损" in text
    assert "系统运行正常" in text
    assert "亏损本身不等于故障" in text


def test_multiple_restarts_are_surfaced(report: DailyReport) -> None:
    """当日重启多次是无人值守语境下用户该知道的事实(也可能是崩溃重启)。"""
    pkg = _package()
    pkg["system_status"]["stability"]["startups"] = 3
    assert "今日系统重启: 3 次" in _text(report._render_self_review(pkg))


def test_single_startup_is_not_noise(report: DailyReport) -> None:
    """只启动一次没什么可说的 —— 不显示, 免得噪音稀释真正重要的行。"""
    assert "今日系统重启" not in _text(report._render_self_review(_package()))


# ---------------------------------------------------------------------------
# 与日报主体集成
# ---------------------------------------------------------------------------


def test_generate_accepts_self_review_and_renders_it(report: DailyReport, tmp_path, monkeypatch) -> None:
    """`generate()` 必须真的把复盘段落写进 `reports/<日期>.md`。"""
    import at70_journal.daily_report as dr

    class _Stub:
        daily_report_dir = str(tmp_path)

    monkeypatch.setattr(dr, "get_settings", lambda: _Stub())

    async def _no_trades(*_a, **_k):
        return []

    async def _zero(*_a, **_k):
        return 0

    monkeypatch.setattr(report, "_trades_since", _no_trades)
    monkeypatch.setattr(report, "_decision_count", _zero)
    monkeypatch.setattr(report, "_strategy_performance", _no_trades)
    monkeypatch.setattr(report, "_order_activity", _no_trades)

    import asyncio

    path = asyncio.run(report.generate("SOLUSDT", self_review=_package()))
    assert path is not None
    body = open(path, encoding="utf-8").read()
    assert "## 今日系统复盘" in body
    assert "交易次数: 8" in body
    # 旧的占位尾巴不该再出现
    assert "待 AI 优化器接入后自动生成" not in body
