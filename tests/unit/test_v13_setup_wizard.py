"""V13 P1 一键进入无人值守向导测试。

任务书把「首次配置」压成**五件事**:

    ① API Key / Secret 已配置   ② 交易模式   ③ 交易资金范围 / 风控边界
    ④ 策略版本                  ⑤ 是否允许进入真实资金运行

并明确: 「除此之外不要求用户手工执行十几个检查命令 / 不要求用户理解
PAPER_TRADING / 不要求用户理解内部状态机」。所以本文件钉死:

- 五要素逐条的**就绪判定与「还差什么」**必须准确(不能虚报就绪, 也不能虚报缺失);
- 向导**只读**: 没有任何写路径, 也没有绕过守卫的入口;
- 高风险项(实盘确认)在向导里**不可绕过** —— 它只能由既有的人工确认流程完成。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from at01_common.trading_mode import ModeResolution, MarketDataSource, TradingMode
from at90_web.web_setup_routes import (
    RISK_BOUNDARY_KEYS,
    build_setup_status,
    setup_router,
)

STEP_KEYS = ("credentials", "mode", "risk", "strategy", "live_confirm")


def _settings(**overrides: Any) -> SimpleNamespace:
    base = dict(
        trading_mode="", paper_trading=True, binance_testnet=True, run_testnet_trading="",
        live_trading_confirm="", mainnet_api_scope_confirmed=False,
        binance_testnet_api_key="", binance_testnet_api_secret="",
        binance_api_key="", binance_api_secret="",
        symbol_list=["SOLUSDT"], model_fields_set=set(),
        risk_max_position_pct=0.3, risk_max_single_order_pct=0.1,
        risk_max_sol_exposure=0.6, risk_max_daily_loss=0.03, risk_max_drawdown=0.15,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _resolved(mode: TradingMode | None, error: str = "") -> ModeResolution:
    return ModeResolution(
        mode=mode, market_data_source=MarketDataSource.TESTNET,
        source="TRADING_MODE" if mode else "", error=error,
    )


def _status(**kw: Any) -> dict[str, Any]:
    return build_setup_status(_settings(**kw.pop("settings", {})), **kw)


# ---------------------------------------------------------------------------
# 五要素
# ---------------------------------------------------------------------------


def test_all_five_steps_are_always_present() -> None:
    """五件事一个都不能少 —— 少了用户就不知道还该确认什么。"""
    out = _status(resolved=_resolved(TradingMode.PAPER))
    assert [s["key"] for s in out["steps"]] == list(STEP_KEYS)
    for s in out["steps"]:
        assert s["label"].strip()
        assert s["detail"].strip()
        assert isinstance(s["done"], bool)


def test_paper_mode_needs_no_credentials() -> None:
    """模拟模式不连私有接口 —— 不该让只想跑模拟的人先去申请 API Key。"""
    out = _status(resolved=_resolved(TradingMode.PAPER))
    cred = next(s for s in out["steps"] if s["key"] == "credentials")
    assert cred["done"] is True
    assert "不需要" in cred["value"]


def test_testnet_mode_requires_testnet_credentials() -> None:
    out = _status(resolved=_resolved(TradingMode.TESTNET))
    cred = next(s for s in out["steps"] if s["key"] == "credentials")
    assert cred["done"] is False
    assert "BINANCE_TESTNET_API_KEY" in cred["action"]


def test_live_mode_requires_mainnet_credentials_and_names_the_scope_requirement() -> None:
    out = _status(resolved=_resolved(TradingMode.LIVE))
    cred = next(s for s in out["steps"] if s["key"] == "credentials")
    assert cred["done"] is False
    assert "BINANCE_API_KEY" in cred["action"]
    assert "提现" in cred["action"]  # 权限范围是实盘密钥的关键提醒


def test_credentials_present_flips_step_to_done() -> None:
    out = build_setup_status(
        _settings(binance_testnet_api_key="k", binance_testnet_api_secret="s"),
        resolved=_resolved(TradingMode.TESTNET),
    )
    cred = next(s for s in out["steps"] if s["key"] == "credentials")
    assert cred["done"] is True


def test_risk_step_lists_only_the_key_boundaries_not_every_param() -> None:
    """任务书: 「不要让用户填写几十个参数」。只展示关键边界, 其余用推荐默认值。"""
    out = _status(resolved=_resolved(TradingMode.PAPER))
    risk = next(s for s in out["steps"] if s["key"] == "risk")
    assert [r["key"] for r in risk["rows"]] == list(RISK_BOUNDARY_KEYS)


def test_risk_step_warns_on_an_aggressive_daily_loss() -> None:
    """日内亏损阈值放宽到 10% 时必须提示风险 —— 这是「资金边界」这一步的意义。"""
    out = build_setup_status(
        _settings(risk_max_daily_loss=0.10), resolved=_resolved(TradingMode.PAPER)
    )
    risk = next(s for s in out["steps"] if s["key"] == "risk")
    assert "偏宽" in risk["detail"]


def test_strategy_step_reports_missing_version_honestly() -> None:
    """读不到版本就说读不到, 不编一个版本号出来。"""
    out = _status(resolved=_resolved(TradingMode.PAPER))
    st = next(s for s in out["steps"] if s["key"] == "strategy")
    assert st["done"] is False
    assert st["action"]


def test_strategy_step_accepts_the_startup_baseline() -> None:
    """启动基线 `active=False`, 但系统确实带着一组明确参数在跑 —— 不能显示成「无」。

    实测踩到过: 向导显示「策略版本: 无」而系统正常运行, 那会让人以为出了故障。
    """
    out = _status(resolved=_resolved(TradingMode.PAPER), active_version="V0.1.0-20260912")
    st = next(s for s in out["steps"] if s["key"] == "strategy")
    assert st["done"] is True
    assert "启动基线" in st["value"]


def test_auto_generated_note_is_not_shown_to_the_user() -> None:
    """系统自动写的英文标识("startup baseline")对用户零信息量, 用中文解释替代。"""
    out = build_setup_status(
        _settings(), resolved=_resolved(TradingMode.PAPER),
        active_version="v1", note="startup baseline",
    )
    st = next(s for s in out["steps"] if s["key"] == "strategy")
    assert "startup baseline" not in st["detail"]
    assert "策略参数" in st["detail"]


def test_human_written_note_is_shown() -> None:
    """人工写下的备注有信息量, 必须原样展示。"""
    out = build_setup_status(
        _settings(), resolved=_resolved(TradingMode.PAPER),
        active_version="v1", note="降低 grid_count 以减小回撤",
    )
    st = next(s for s in out["steps"] if s["key"] == "strategy")
    assert "降低 grid_count" in st["detail"]


def test_strategy_step_distinguishes_activated_version_from_baseline() -> None:
    """两种来源的文案必须不同 —— 用户要知道现在跑的是人工激活过的版本还是自动基线。"""
    baseline = _status(resolved=_resolved(TradingMode.PAPER), active_version="v1")
    activated = build_setup_status(
        _settings(), resolved=_resolved(TradingMode.PAPER),
        active_version="v1", version_activated=True,
    )
    b = next(s for s in baseline["steps"] if s["key"] == "strategy")["value"]
    a = next(s for s in activated["steps"] if s["key"] == "strategy")["value"]
    assert b != a
    assert "已激活" in a


# ---------------------------------------------------------------------------
# 高风险项: 实盘确认不可绕过
# ---------------------------------------------------------------------------


def test_live_confirmation_is_not_required_outside_live_mode() -> None:
    out = _status(resolved=_resolved(TradingMode.PAPER), active_version="v1")
    lv = next(s for s in out["steps"] if s["key"] == "live_confirm")
    assert lv["required"] is False
    assert lv["done"] is True
    assert out["ready"] is True


def test_live_mode_is_not_ready_without_explicit_confirmation() -> None:
    """向导**不能**替用户确认真实资金 —— 那是必须由人承担的责任。"""
    out = build_setup_status(
        _settings(binance_api_key="k", binance_api_secret="s"),
        resolved=_resolved(TradingMode.LIVE), active_version="v1",
    )
    lv = next(s for s in out["steps"] if s["key"] == "live_confirm")
    assert lv["required"] is True
    assert lv["done"] is False
    assert out["ready"] is False
    assert "真实资金运行确认" in out["conclusion"]["missing"]


def test_live_mode_ready_once_both_confirms_are_present() -> None:
    out = build_setup_status(
        _settings(binance_api_key="k", binance_api_secret="s",
                  live_trading_confirm="true", mainnet_api_scope_confirmed=True),
        resolved=_resolved(TradingMode.LIVE), active_version="v1",
    )
    assert out["ready"] is True
    assert "无人值守" in out["conclusion"]["title"]


# ---------------------------------------------------------------------------
# 结论
# ---------------------------------------------------------------------------


def test_conclusion_lists_exactly_what_is_missing() -> None:
    out = _status(resolved=_resolved(TradingMode.TESTNET), active_version="")
    assert out["ready"] is False
    assert set(out["conclusion"]["missing"]) == {"API Key / Secret", "策略版本"}


def test_unresolvable_mode_is_not_ready_and_says_why() -> None:
    """模式解析不出来(fail-closed)时, 向导必须说清原因, 不能显示「就绪」。"""
    out = _status(resolved=_resolved(None, error="旧配置无法确定模式: ..."))
    assert out["ready"] is False
    assert "无法确定模式" in out["mode_error"]


def test_ready_conclusion_promises_unattended_operation() -> None:
    out = _status(resolved=_resolved(TradingMode.PAPER), active_version="v1")
    assert out["ready"] is True
    assert "无人值守" in out["conclusion"]["title"]
    assert "自动处理普通异常" in out["conclusion"]["summary"]


def test_wizard_offers_exactly_three_modes() -> None:
    out = _status(resolved=_resolved(TradingMode.PAPER))
    assert [m["label"] for m in out["modes"]] == ["模拟", "测试", "实盘"]


# ---------------------------------------------------------------------------
# 只读契约
# ---------------------------------------------------------------------------


def test_wizard_exposes_no_write_endpoint() -> None:
    """向导**只读**。另起一套写路径等于在守卫旁边开一个旁门。"""
    for route in setup_router.routes:
        methods = getattr(route, "methods", set()) or set()
        assert methods <= {"GET", "HEAD"}, f"{route.path} 暴露了写方法 {methods}"


def test_wizard_never_returns_credentials() -> None:
    """密钥绝不出现在响应里 —— 连「值」都不能有, 只能有「已配置/未配置」。"""
    out = build_setup_status(
        _settings(binance_testnet_api_key="SUPERSECRETKEY", binance_testnet_api_secret="shh"),
        resolved=_resolved(TradingMode.TESTNET),
    )
    blob = repr(out)
    assert "SUPERSECRETKEY" not in blob
    assert "shh" not in blob


def test_wizard_does_not_rejudge_trading_permission() -> None:
    """交易许可来自 operator-status(其本身取自 TradingGate), 向导只搬运不重算。"""
    out = build_setup_status(
        _settings(), resolved=_resolved(TradingMode.PAPER),
        status={"can_buy": False, "can_sell": True, "equity": 123.0,
                "trading_mode_label": "模拟", "risk_level": "warning",
                "health_summary": {"conclusion": "x"}, "runtime": {"reconciled": False}},
    )
    assert out["status"]["can_buy"] is False
    assert out["status"]["can_sell"] is True


@pytest.mark.parametrize("mode", [TradingMode.PAPER, TradingMode.TESTNET, TradingMode.LIVE])
def test_status_label_is_never_the_raw_mode_combo(mode: TradingMode) -> None:
    """五要素里出现的一律是三模式名, 不是 live_mainnet / paper_testnet 这类底层组合。

    注意**不禁止** `BINANCE_TESTNET_API_KEY` 这类**环境变量名** —— 那是用户真的要填的东西,
    把环境变量名藏起来反而让他无从下手。要藏的是「模式」这个概念的内部表示。
    """
    out = _status(resolved=_resolved(mode))
    blob = repr(out["steps"])
    for token in ("live_mainnet", "paper_mainnet", "live_testnet", "paper_testnet"):
        assert token not in blob, token
