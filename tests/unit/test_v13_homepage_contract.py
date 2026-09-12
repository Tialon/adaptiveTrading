"""V13 P0/P1 首页与健康报告契约测试。

任务书的验收原话:

    用户打开页面后 5 秒内知道系统是否正常。
    如果用户不需要做任何事情: **明确告诉用户「无需操作」。**
    所有页面遵循: 结论 > 原因 > 影响 > 系统动作 > 用户动作

所以本文件钉死三件事:
1. `/api/operator-status` 必须带上首屏需要的**全部**字段(缺一个页面就会露出空白);
2. 健康报告的每一项都必须标明**归谁管**(AUTO / AUTO_BLOCK / HUMAN) —— 页面据此决定
   要不要打扰用户, 这个判断只能有一处;
3. 三模式口径: 首屏字段只出现「模拟/测试/实盘」, 底层四组合收进 `advanced`。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from at90_web.web_health_report import (
    HANDLING_AUTO,
    HANDLING_AUTO_BLOCK,
    HANDLING_HUMAN,
    build_health_report,
    summarize_health_report,
)
from at90_web.web_operator_status import build_operator_status

# 首屏结论卡直接消费的字段 —— 少任何一个, 页面就会显示空白或退化成技术字段
REQUIRED_TOP_LEVEL = (
    "narrative", "notice_level", "notice_level_label",
    "health_report", "health_summary", "attention", "today",
    "trading_mode", "trading_mode_label", "advanced",
    "can_buy", "can_sell", "switches", "guard_override",
)
REQUIRED_NARRATIVE = (
    "level", "level_label", "tone", "title", "conclusion",
    "cause", "impact", "system_actions", "user_action", "next_step", "requires_human",
)


def _settings(**overrides: Any) -> SimpleNamespace:
    base = dict(
        app_name="adaptiveTrading", app_version="0.1.0", git_sha="", image_tag="",
        paper_trading=True, binance_testnet=True, live_trading_confirm="",
        mainnet_api_scope_confirmed=False, web_admin_token="", admin_auth_disabled=False,
        trading_mode="", run_testnet_trading="", guard_override="",
    )
    base.update(overrides)
    # validate() 在健康报告里被调用; 桩用一个「无问题」的最小实现
    base["validate"] = lambda: []
    return SimpleNamespace(**base)


def _health(status: str = "TRADING", **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "status": status, "state": status, "running": True, "uptime_seconds": 120.0,
        "lifecycle": {"state": "TRADING", "reason": ""},
        "risk": {"state": "NORMAL", "reason": ""},
        "kill_switch": {"armed": False, "reason": ""},
        "breaker": {"open": False, "reason": "", "fund_action": "NONE"},
        "reconcile": {"reconciled": True, "last_reconcile_at": 1.0},
        "market": {"connection_ok": True, "data_healthy": True, "ws_silence_seconds": 0.0},
        "exchange": {"healthy": True},
        "tasks": {"total": 9, "running": 9, "failed": 0, "failure_count": 0},
        "trade": {"orders_total": 0, "orders_failed": 0, "order_failure_rate": 0.0},
        "can_buy": True, "can_sell": True,
        "buy_block_reason": "", "sell_block_reason": "",
        "last_error": None,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 首屏字段齐备
# ---------------------------------------------------------------------------


def test_status_payload_has_every_first_screen_field() -> None:
    body = build_operator_status(_settings(), _health())
    for key in REQUIRED_TOP_LEVEL:
        assert key in body, f"首屏缺少字段 {key}"
    for key in REQUIRED_NARRATIVE:
        assert key in body["narrative"], f"五段式缺少 {key}"


def test_first_screen_narrative_is_never_blank() -> None:
    """系统还没完全启动时也不能白屏 —— 这是「5 秒内知道是否正常」的底线。"""
    for payload in (_health(), {}, {"status": "SAFE"}):
        body = build_operator_status(_settings(), payload)
        n = body["narrative"]
        for key in ("title", "conclusion", "cause", "impact", "user_action", "next_step"):
            assert str(n[key]).strip(), key


def test_status_endpoint_never_rejudges_trading_permission() -> None:
    """单一权威: can_buy/can_sell 必须原样透传快照, 本层不得自己算一遍。"""
    body = build_operator_status(_settings(), _health(can_buy=False, can_sell=True))
    assert body["can_buy"] is False
    assert body["can_sell"] is True


# ---------------------------------------------------------------------------
# 三模式口径
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "paper,testnet,expected",
    [
        (True, True, "模拟"),
        (False, True, "测试"),
        (False, False, "实盘"),
    ],
)
def test_first_screen_uses_three_modes_only(paper: bool, testnet: bool, expected: str) -> None:
    body = build_operator_status(
        _settings(paper_trading=paper, binance_testnet=testnet), _health()
    )
    assert body["trading_mode_label"] == expected
    # 底层组合仍可得, 但只出现在 advanced(高级诊断)里
    assert body["advanced"]["mode"] in (
        "paper_testnet", "paper_mainnet", "live_testnet", "live_mainnet"
    )
    assert body["advanced"]["note"]


def test_narrative_uses_the_three_mode_label_not_the_combo() -> None:
    body = build_operator_status(_settings(paper_trading=False, binance_testnet=False), _health())
    assert "实盘" in body["narrative"]["conclusion"]
    assert "live_mainnet" not in body["narrative"]["conclusion"]


# ---------------------------------------------------------------------------
# 健康报告: 每项都要标明「归谁管」
# ---------------------------------------------------------------------------


def test_health_items_all_declare_handling() -> None:
    items = build_health_report(_health(), settings=_settings())
    assert items, "健康报告不能为空"
    for i in items:
        assert i["handling"] in (HANDLING_AUTO, HANDLING_AUTO_BLOCK, HANDLING_HUMAN), i
        assert i["handling_label"].strip()
        assert i["label"].strip()
        assert i["conclusion"].strip()


def test_all_healthy_gives_a_single_conclusion_without_listing_pass_items() -> None:
    """任务书: 没有问题就**不要显示一堆绿色 PASS**, 直接说「系统正常, 无需操作」。"""
    items = build_health_report(_health(), settings=_settings(), db_ok=True)
    summary = summarize_health_report(items)
    assert summary["ok"] is True
    assert summary["needs_human"] is False
    assert "无需操作" in summary["conclusion"]


def test_auto_recoverable_fault_does_not_ask_for_a_human() -> None:
    """行情静默 / 对账未完成 / 任务崩溃 —— 系统自己会恢复, **不该打扰用户**。"""
    items = build_health_report(
        _health(
            status="RECOVERY",
            lifecycle={"state": "RECOVERY", "reason": "对账 RECOVERY_REQUIRED"},
            reconcile={"reconciled": False, "last_reconcile_at": None},
            market={"connection_ok": False, "data_healthy": False, "ws_silence_seconds": 42.0},
            tasks={"total": 9, "running": 8, "failed": 1, "failure_count": 1},
            can_buy=False, buy_block_reason="对账未通过",
        ),
        settings=_settings(), db_ok=True,
    )
    summary = summarize_health_report(items)
    assert summary["ok"] is False
    assert summary["needs_human"] is False, "可自愈的故障不该要求人工"
    assert "自动处理" in summary["conclusion"]


def test_kill_switch_makes_risk_item_require_a_human() -> None:
    """急停冻结 = 高风险 KILL, 属任务书列出的「必须人工确认」项。"""
    items = build_health_report(
        _health(
            status="KILLED", risk={"state": "KILLED", "reason": "回撤 15%"},
            kill_switch={"armed": True, "reason": "回撤 15%"},
            can_buy=False, can_sell=False, buy_block_reason="急停中: 回撤 15%",
        ),
        settings=_settings(), db_ok=True,
    )
    summary = summarize_health_report(items)
    assert summary["needs_human"] is True
    assert "需要你的确认" in summary["conclusion"]
    risk_item = next(i for i in items if i["key"] == "risk")
    assert risk_item["handling"] == HANDLING_HUMAN
    assert risk_item["human_action"]


def test_observation_only_metric_never_makes_the_system_look_broken() -> None:
    """订单失败率是**喂 AI 复盘**的观察项 —— 一笔订单没成交不代表系统不正常。

    若把它算进总结, 页面会在一切正常时显示「系统正在自动处理」, 那是虚报异常,
    与「无事就明说无需操作」直接冲突。
    """
    items = build_health_report(
        _health(trade={"orders_total": 1, "orders_failed": 1, "order_failure_rate": 1.0}),
        settings=_settings(), db_ok=True,
    )
    orders = next(i for i in items if i["key"] == "orders")
    assert orders["ok"] is False        # 指标本身如实反映
    assert orders["blocking"] is False  # 但不阻断结论
    summary = summarize_health_report(items)
    assert summary["ok"] is True
    assert "无需操作" in summary["conclusion"]


def test_every_item_declares_blocking_explicitly() -> None:
    """`blocking` 必须有值 —— 默认 True 是 fail-closed, 但显式标注才看得出意图。"""
    for i in build_health_report(_health(), settings=_settings()):
        assert isinstance(i["blocking"], bool), i["key"]


def test_database_unreachable_warns_but_is_not_a_human_decision() -> None:
    items = build_health_report(_health(), settings=_settings(), db_ok=False)
    db_item = next(i for i in items if i["key"] == "database")
    assert db_item["ok"] is False
    assert db_item["handling"] == HANDLING_AUTO_BLOCK  # 系统会阻止交易, 但不要求人拍板


def test_unprobed_database_says_so_instead_of_pretending_ok() -> None:
    """探测不到 ≠ 正常。宁可显示「未单独探测」, 也不能冒充「正常」。"""
    items = build_health_report(_health(), settings=_settings(), db_ok=None)
    db_item = next(i for i in items if i["key"] == "database")
    assert "未单独探测" in db_item["conclusion"]


def test_config_problems_are_reported_as_human_work() -> None:
    settings = _settings()
    settings.validate = lambda: ["RISK_MAX_POSITION_PCT 非法", "grid_count 非法"]
    items = build_health_report(_health(), settings=settings)
    cfg = next(i for i in items if i["key"] == "config")
    assert cfg["ok"] is False
    assert cfg["handling"] == HANDLING_HUMAN
    assert "RISK_MAX_POSITION_PCT 非法" in cfg["detail"]


def test_config_validation_crash_is_reported_not_hidden() -> None:
    """校验器自己炸了也必须如实显示为异常 —— 不能吞掉后显示「配置正常」。"""
    settings = _settings()

    def _boom() -> list[str]:
        raise RuntimeError("validator down")

    settings.validate = _boom
    items = build_health_report(_health(), settings=settings)
    cfg = next(i for i in items if i["key"] == "config")
    assert cfg["ok"] is False
    assert "validator down" in cfg["detail"]


# ---------------------------------------------------------------------------
# 需要你关注的事
# ---------------------------------------------------------------------------


def test_attention_is_empty_when_everything_is_fine() -> None:
    """系统正常时必须明说「无」, 而不是列一堆不需要用户做的事。"""
    body = build_operator_status(_settings(web_admin_token="tok"), _health())
    assert body["attention"] == []


def test_attention_surfaces_auth_disabled_and_live_confirm() -> None:
    body = build_operator_status(
        _settings(admin_auth_disabled=True, web_admin_token="",
                  paper_trading=False, binance_testnet=False, live_trading_confirm="true"),
        _health(),
    )
    titles = [i["title"] for i in body["attention"]]
    assert any("鉴权" in t for t in titles), titles
    assert any("真实资金" in t for t in titles), titles


def test_attention_does_not_list_self_healing_states() -> None:
    """「系统正在自动恢复」不是「需要你关注」—— 把它列进去等于让用户天天盯着看。"""
    body = build_operator_status(
        _settings(web_admin_token="tok"),
        _health(status="RECOVERY", lifecycle={"state": "RECOVERY", "reason": "对账未通过"},
                risk={"state": "RECOVERY_CHECK", "reason": "对账未通过"},
                can_buy=False, buy_reason="对账未通过"),
    )
    assert body["narrative"]["requires_human"] is False
    assert body["attention"] == []


def test_attention_lists_human_required_narrative() -> None:
    body = build_operator_status(
        _settings(web_admin_token="tok"),
        _health(status="KILLED", risk={"state": "KILLED", "reason": "回撤 15%"},
                kill_switch={"armed": True, "reason": "回撤 15%"},
                can_buy=False, can_sell=False),
    )
    assert body["narrative"]["requires_human"] is True
    assert any(i["level"] == "ACTION_REQUIRED" for i in body["attention"])


# ---------------------------------------------------------------------------
# 今日统计
# ---------------------------------------------------------------------------


def test_today_block_is_complete_even_with_no_trades() -> None:
    """今天还没交易时不能显示「胜率 0%」—— 那会被读成「全亏」。"""
    body = build_operator_status(_settings(), _health())
    today = body["today"]
    for key in ("trades", "pnl", "pnl_pct", "win_rate", "max_drawdown_pct",
                "auto_recoveries", "ws_reconnects", "human_interventions", "summary"):
        assert key in today, key
    assert today["trades"] == 0
    assert "还没有交易" in today["summary"]


def test_today_block_renders_actual_counts() -> None:
    body = build_operator_status(
        _settings(), _health(),
        extras={"today": {"trades": 4, "pnl": 12.5, "pnl_pct": 1.2, "win_rate": 0.75}},
    )
    assert body["today"]["trades"] == 4
    assert "4 次交易" in body["today"]["summary"]
    assert "75%" in body["today"]["summary"]
