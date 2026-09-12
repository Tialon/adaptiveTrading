"""V13 P0「用户状态测试」—— 每一种内部状态都必须有人话五段式。

任务书的验收原话:

    每一种内部状态都有: 人话标题 / 原因 / 影响 / 系统动作 / 用户动作

所以本文件的**主测试是穷举**: 7 个运行时状态 × 10 个生命周期态 × 5 个风险态 ×
急停/熔断/对账/任务异常的各种组合, 逐条断言五段式**非空**且**说到做到**。

另有两类"不能骗人"的断言:
- `无需操作` 只允许出现在**系统真的会自愈**的状态上;
- 需要人的状态必须**明确说要人做什么**, 不能只说"异常"。
"""

from __future__ import annotations

import itertools
from typing import Any

import pytest

from at01_common.operator_narrative import (
    ALL_KILL_ORIGINS,
    LEVEL_ACTION_REQUIRED,
    LEVEL_DEGRADED,
    LEVEL_KILLED,
    LEVEL_NORMAL,
    LEVEL_NOTICE,
    LEVEL_SEVERITY,
    MAJOR_FUND_KILL_ORIGINS,
    KILL_ORIGIN_MANUAL,
    SELF_HEALABLE_KILL_ORIGINS,
    classify_notice_level,
    explain,
    explain_block_reason,
    explain_trade,
    kill_requires_human,
    resolve_situation,
    summarize_trade,
)
from at01_common.runtime_health import (
    STATUS_DEGRADED,
    STATUS_KILLED,
    STATUS_PAUSED,
    STATUS_RECOVERY,
    STATUS_REDUCE_ONLY,
    STATUS_SAFE,
    STATUS_TRADING,
)

RUNTIME_STATUSES = (
    STATUS_SAFE, STATUS_TRADING, STATUS_DEGRADED,
    STATUS_REDUCE_ONLY, STATUS_PAUSED, STATUS_RECOVERY, STATUS_KILLED,
)
LIFECYCLE_STATES = (
    "INIT", "WARMING_UP", "SYNCING", "SELF_CHECK", "READY",
    "TRADING", "DEGRADED", "RECOVERY", "SAFE_MODE", "STOPPED",
)
RISK_STATES = ("NORMAL", "REDUCE_ONLY", "PAUSED", "KILLED", "RECOVERY_CHECK")

# 五段式的每一条都是「用户要读的」, 缺任何一条这个页面就退化回技术字段
REQUIRED_TEXT_KEYS = ("title", "conclusion", "cause", "impact", "user_action", "next_step")


def health(
    *,
    status: str = STATUS_TRADING,
    lifecycle: str = "TRADING",
    lifecycle_reason: str = "",
    risk: str = "NORMAL",
    risk_reason: str = "",
    kill_armed: bool = False,
    kill_reason: str = "",
    breaker_open: bool = False,
    breaker_reason: str = "",
    fund_action: str = "NONE",
    can_buy: bool = True,
    can_sell: bool = True,
    buy_reason: str = "",
    sell_reason: str = "",
    tasks_failed: int = 0,
    reconciled: bool = True,
) -> dict[str, Any]:
    """构造 `build_runtime_health()` 形状的快照(字段与 runtime_health.py 一一对应)。"""
    return {
        "status": status,
        "state": status,
        "lifecycle": {"state": lifecycle, "reason": lifecycle_reason},
        "risk": {"state": risk, "reason": risk_reason},
        "kill_switch": {"armed": kill_armed, "reason": kill_reason},
        "breaker": {
            "open": breaker_open, "reason": breaker_reason, "fund_action": fund_action,
        },
        "reconcile": {"reconciled": reconciled, "last_reconcile_at": None},
        "market": {"connection_ok": True, "data_healthy": True},
        "exchange": {"healthy": True},
        "tasks": {"total": 9, "running": 9, "failed": tasks_failed, "failure_count": 0},
        "can_buy": can_buy,
        "can_sell": can_sell,
        "buy_block_reason": buy_reason,
        "sell_block_reason": sell_reason,
    }


# ---------------------------------------------------------------------------
# 穷举: 任何内部状态组合都要产出完整五段式
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,lifecycle,risk",
    list(itertools.product(RUNTIME_STATUSES, LIFECYCLE_STATES, RISK_STATES)),
)
def test_every_state_yields_complete_narrative(status: str, lifecycle: str, risk: str) -> None:
    """7 × 10 × 5 = 350 种组合, 每一种的六段文本都必须非空。"""
    out = explain(health(status=status, lifecycle=lifecycle, risk=risk))

    for key in REQUIRED_TEXT_KEYS:
        assert isinstance(out.get(key), str), f"{key} 必须是字符串"
        assert out[key].strip(), f"{status}/{lifecycle}/{risk} 的 {key} 为空"

    assert out["system_actions"], f"{status}/{lifecycle}/{risk} 没有任何「系统动作」"
    assert all(a.strip() for a in out["system_actions"])
    assert out["level"] in LEVEL_SEVERITY


@pytest.mark.parametrize("status", RUNTIME_STATUSES)
def test_level_is_declared_and_labelled(status: str) -> None:
    out = explain(health(status=status))
    assert out["level_label"].strip()
    assert out["tone"].strip()
    assert out["level_label"] != "未知"


def test_empty_health_does_not_raise() -> None:
    """系统未完全启动时也必须给出可读结论, 而不是抛异常/空白页。"""
    for payload in ({}, None):
        out = explain(payload or {})
        for key in REQUIRED_TEXT_KEYS:
            assert out[key].strip()


@pytest.mark.parametrize("origin", list(ALL_KILL_ORIGINS) + ["", "SOMETHING_ELSE"])
def test_killed_level_follows_kill_origin(origin: str) -> None:
    """急停来源决定「要不要人」——仅可自愈来源免人工, 其余一律要人(fail-closed)。"""
    out = explain(
        health(
            status=STATUS_KILLED, lifecycle="TRADING", risk="KILLED",
            kill_armed=True, kill_reason="对账矩阵 KILLED",
            can_buy=False, can_sell=False, buy_reason="急停中: 对账矩阵 KILLED",
        ),
        kill_origin=origin,
    )
    if origin in SELF_HEALABLE_KILL_ORIGINS:
        assert out["level"] == LEVEL_DEGRADED
        assert out["requires_human"] is False
        assert "无需操作" in out["user_action"]
    else:
        assert out["requires_human"] is True
        assert out["level"] == LEVEL_KILLED
        assert "确认" in out["user_action"]


def test_kill_requires_human_is_fail_closed() -> None:
    assert kill_requires_human("") is True
    assert kill_requires_human("MANUAL") is True
    assert kill_requires_human("SOMETHING_NEW") is True
    for origin in SELF_HEALABLE_KILL_ORIGINS:
        assert kill_requires_human(origin) is False


@pytest.mark.parametrize("origin", sorted(MAJOR_FUND_KILL_ORIGINS))
def test_major_fund_kill_origins_always_require_human(origin: str) -> None:
    """重大资金异常(对账/权益/账务)**不交给自动恢复** —— 系统无法自证账本没错。"""
    assert kill_requires_human(origin) is True


def test_self_healable_set_is_a_strict_subset_and_never_overlaps_major_fund() -> None:
    """可自愈来源必须是全集的真子集, 且与重大资金异常零交集(防将来改错)。"""
    assert SELF_HEALABLE_KILL_ORIGINS < set(ALL_KILL_ORIGINS)
    assert not (SELF_HEALABLE_KILL_ORIGINS & MAJOR_FUND_KILL_ORIGINS)
    assert KILL_ORIGIN_MANUAL not in SELF_HEALABLE_KILL_ORIGINS


def test_fund_kill_always_requires_human_even_when_self_healable_origin() -> None:
    """资金熔断 KILL 档 + 可自愈来源 → 仍要人(资金异常优先于来源)。"""
    out = explain(
        health(
            status=STATUS_KILLED, lifecycle="TRADING", risk="KILLED",
            kill_armed=True, can_buy=False, can_sell=False,
            breaker_open=True, fund_action="KILL", breaker_reason="权益漂移 12%",
        ),
        kill_origin="AUTO_RECONCILE",
    )
    assert out["requires_human"] is True
    assert out["level"] == LEVEL_KILLED


# ---------------------------------------------------------------------------
# 「无需操作」不能乱说
# ---------------------------------------------------------------------------


def test_trading_is_normal_and_says_no_action_needed() -> None:
    out = explain(health(), mode_label="模拟")
    assert out["level"] == LEVEL_NORMAL
    assert out["requires_human"] is False
    assert "无需操作" in out["user_action"]
    assert "无需操作" in out["conclusion"]


def test_auto_recovering_states_say_no_action_needed() -> None:
    """真正会自动恢复的状态, 才允许说「无需操作, 系统正在自动恢复」。"""
    cases = [
        ("RECOVERY", "RECOVERY", "RECOVERY_CHECK"),
        ("DEGRADED", "DEGRADED", "NORMAL"),
        ("REDUCE_ONLY", "TRADING", "REDUCE_ONLY"),
        ("PAUSED", "TRADING", "PAUSED"),
    ]
    for status, lifecycle, risk in cases:
        out = explain(health(status=status, lifecycle=lifecycle, risk=risk,
                             can_buy=False, buy_reason="降级中"))
        assert out["level"] == LEVEL_DEGRADED, status
        assert out["requires_human"] is False, status
        assert "无需操作" in out["user_action"], status


def test_safe_mode_does_not_promise_auto_recovery() -> None:
    """SAFE_MODE 当前**没有**自动退出路径(exit_safe_mode 无生产调用者), 所以不能谎称自愈。"""
    out = explain(
        health(status=STATUS_KILLED, lifecycle="SAFE_MODE", risk="KILLED",
               kill_armed=True, can_buy=False, can_sell=False, buy_reason="安全模式"),
        kill_origin="AUTO_RECONCILE",  # 哪怕来源是自动, 安全模式仍要人
    )
    assert out["situation"] == "SAFE_MODE"
    assert out["level"] == LEVEL_ACTION_REQUIRED
    assert out["requires_human"] is True
    assert "无需操作" not in out["user_action"]


def test_stopped_requires_human_and_never_claims_self_healing() -> None:
    out = explain(health(status=STATUS_KILLED, lifecycle="STOPPED", risk="KILLED"))
    assert out["situation"] == "STOPPED"
    assert out["level"] == LEVEL_ACTION_REQUIRED
    assert out["requires_human"] is True
    assert "重新启动服务" in out["user_action"]


# ---------------------------------------------------------------------------
# 具体原因要取自真实字段, 不能只给套话
# ---------------------------------------------------------------------------


def test_real_reason_fields_are_surfaced() -> None:
    out = explain(
        health(
            status=STATUS_PAUSED, lifecycle="DEGRADED", lifecycle_reason="对账 RECOVERY_REQUIRED",
            risk="PAUSED", risk_reason="权益漂移超阈值",
            breaker_open=True, breaker_reason="drift=0.032", fund_action="PAUSE",
            can_buy=False, buy_reason="资金熔断: PAUSE",
        )
    )
    blob = out["cause"] + out["impact"] + " ".join(out["system_actions"])
    assert "对账 RECOVERY_REQUIRED" in blob
    assert "权益漂移超阈值" in blob
    assert "drift=0.032" in blob


def test_capability_loss_is_listed_explicitly() -> None:
    """买卖两侧被暂停要如实列出 —— 尤其 REDUCE_ONLY 的关键信息是「还能卖」。"""
    out = explain(health(status=STATUS_REDUCE_ONLY, lifecycle="TRADING", risk="REDUCE_ONLY",
                         can_buy=False, can_sell=True, buy_reason="仅减仓: 回撤 8%"))
    assert "已暂停买入(BUY)" in out["system_actions"]
    assert "已暂停卖出(SELL)" not in out["system_actions"]


def test_failed_tasks_are_reported_and_downgrade_a_normal_snapshot() -> None:
    """后台任务异常属「系统自动处理」, 但也不能把 TRADING 无脑显示成一切正常。"""
    out = explain(health(status=STATUS_TRADING, tasks_failed=2))
    assert out["level"] == LEVEL_NOTICE
    assert any("后台任务异常" in a for a in out["system_actions"])


# ---------------------------------------------------------------------------
# 不泄露内部字段名(与 test_v12_dashboard_usability 同一契约)
# ---------------------------------------------------------------------------

_LEAKY_TOKENS = (
    "can_buy", "can_sell", "buy_block_reason", "sell_block_reason",
    "lifecycle", "risk_state", "kill_switch", "fund_action",
    "RECOVERY_CHECK", "SAFE_MODE", "REDUCE_ONLY", "WARMING_UP", "SELF_CHECK",
)


@pytest.mark.parametrize(
    "status,lifecycle,risk",
    list(itertools.product(RUNTIME_STATUSES, LIFECYCLE_STATES, RISK_STATES)),
)
def test_narrative_does_not_leak_internal_field_names(
    status: str, lifecycle: str, risk: str
) -> None:
    """操作者看到的每一段都不能出现内部字段名/枚举值。"""
    out = explain(health(status=status, lifecycle=lifecycle, risk=risk))
    blob = " ".join(
        [out["title"], out["conclusion"], out["cause"], out["impact"], out["user_action"]]
        + list(out["system_actions"])
    )
    for token in _LEAKY_TOKENS:
        assert token not in blob, f"{status}/{lifecycle}/{risk} 泄露了内部字段 {token}"


# ---------------------------------------------------------------------------
# 分级
# ---------------------------------------------------------------------------


def test_level_severity_is_strictly_ordered() -> None:
    ordered = [LEVEL_NORMAL, LEVEL_NOTICE, LEVEL_DEGRADED, LEVEL_ACTION_REQUIRED, LEVEL_KILLED]
    values = [LEVEL_SEVERITY[x] for x in ordered]
    assert values == sorted(values)
    assert len(set(values)) == len(values)


def test_situation_distinguishes_states_that_share_runtime_status() -> None:
    """STOPPED / SAFE_MODE / 急停 都被分类器归并成 KILLED, 但解释必须区分。"""
    assert resolve_situation(health(status=STATUS_KILLED, lifecycle="STOPPED")) == "STOPPED"
    assert resolve_situation(health(status=STATUS_KILLED, lifecycle="SAFE_MODE")) == "SAFE_MODE"
    assert resolve_situation(health(status=STATUS_KILLED, lifecycle="TRADING")) == "KILLED"
    assert resolve_situation(health(status=STATUS_TRADING, lifecycle="TRADING")) == "TRADING"


@pytest.mark.parametrize("status", RUNTIME_STATUSES)
def test_classify_never_returns_unknown_level(status: str) -> None:
    assert classify_notice_level(health(status=status)) in LEVEL_META_KEYS


LEVEL_META_KEYS = set(LEVEL_SEVERITY)


# ---------------------------------------------------------------------------
# 阻断原因翻译
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    [
        "系统已停止", "安全模式", "启动中(WARMING_UP)", "恢复中", "降级中",
        "行情连接未就绪", "对账未通过", "急停中: 回撤 15%", "恢复核验中: 对账",
        "风险暂停", "仅减仓", "资金熔断: KILL", "行情数据不健康", "交易所不健康",
        "关键后台任务未运行", "系统停机中", "急停中: 人工急停", "",
    ],
)
def test_block_reason_translation_is_always_complete(reason: str) -> None:
    out = explain_block_reason(reason)
    assert out["cause"].strip()
    assert out["impact"].strip()
    assert out["user_action"].strip()


def test_block_reason_matches_most_specific_rule_first() -> None:
    """「资金熔断」比「熔断」具体, 必须命中前者 —— 顺序错了会给错动作。"""
    assert "资金级熔断" in explain_block_reason("资金熔断: KILL")["cause"]
    assert "资金级熔断" not in explain_block_reason("熔断冷却中")["cause"]


def test_block_reason_of_kill_points_at_account_check() -> None:
    out = explain_block_reason("急停中: 人工急停")
    assert "现场" in out["user_action"] or "确认" in out["user_action"]


# ---------------------------------------------------------------------------
# 交易结论
# ---------------------------------------------------------------------------


def test_explain_trade_buy_is_structured_and_readable() -> None:
    trade = explain_trade(
        side="BUY", symbol="SOLUSDT", status="FILLED",
        price=150.25, quantity=3.0, quote=450.75,
        slippage=0.0012, latency_ms=340, score=86,
        regime="TRENDING", trend="上升", money_flow="正向",
        risk_notes=["单笔风险: 正常", "日亏损: 正常"],
    )
    assert trade["title"] == "买入 SOLUSDT"
    assert trade["result"] == "成功"
    assert any("策略评分" in r for r in trade["why"])
    assert any("滑点" in e for e in trade["execution"])
    assert any("耗时" in e for e in trade["execution"])
    # AI 要能直接消费 -> 结构化字段必须在
    for key in ("side", "symbol", "price", "quantity", "quote", "why", "risk", "execution"):
        assert key in trade


def test_explain_trade_sell_uses_sell_wording() -> None:
    trade = explain_trade(side="SELL", symbol="SOLUSDT", status="FILLED")
    assert trade["title"].startswith("卖出")
    assert trade["side"] == "SELL"


def test_explain_trade_handles_missing_optional_fields() -> None:
    trade = explain_trade(side="BUY", symbol="SOLUSDT")
    assert trade["result"]  # 没有 status 也不能是空的
    assert trade["why"] == []
    assert summarize_trade(trade).strip()


def test_summarize_trade_is_one_line() -> None:
    trade = explain_trade(side="BUY", symbol="SOLUSDT", status="FILLED",
                          price=150.0, quantity=2.0)
    line = summarize_trade(trade)
    assert "\n" not in line
    assert "买入 SOLUSDT" in line
    assert "150.0" in line
