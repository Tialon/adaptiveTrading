"""Dashboard 易用性 / 操作者状态聚合测试(只读展示层)。

覆盖:
- 四种运行模式判定(paper/live × testnet/mainnet);
- 各状态(KILLED / RECOVERY / PAUSED / REDUCE_ONLY / DEGRADED / TRADING / SAFE)
  的 summary 与 next_action 是人话, 且不含内部字段名;
- 交易闸门未就绪(health 为空)时仍返回稳定结构, 不抛异常;
- 交易许可**不得**由本层重新判定(必须原样透传 runtime health 的 can_buy/can_sell);
- `WEB_ADMIN_TOKEN` 只暴露「已配置/未配置」, 绝不回显其值。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from at10_web.web_operator_status import (
    DANGEROUS_ACTIONS,
    MODE_META,
    build_operator_status,
    explain_switches,
    resolve_mode,
    suggest_next_action,
)


def _settings(**overrides: Any) -> SimpleNamespace:
    """构造最小 settings 桩(不读 .env, 避免受本机环境影响)。"""
    base = dict(
        app_name="adaptiveTrading",
        app_version="0.1.0",
        paper_trading=True,
        binance_testnet=True,
        live_trading_confirm="",
        mainnet_api_scope_confirmed=False,
        web_admin_token="",
        admin_auth_disabled=False,
        git_sha="",
        image_tag="",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _health(**overrides: Any) -> dict[str, Any]:
    """构造 runtime health 快照桩(只含本层读取的字段)。"""
    base: dict[str, Any] = {
        "status": "TRADING",
        "running": True,
        "uptime_seconds": 12.5,
        "can_buy": True,
        "can_sell": True,
        "buy_block_reason": "",
        "sell_block_reason": "",
        "reconcile": {"reconciled": True, "last_reconcile_at": 1789132733.0},
        "tasks": {"failed": 0},
        "last_error": None,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 模式判定
# ---------------------------------------------------------------------------


class TestResolveMode:
    @pytest.mark.parametrize(
        ("paper", "testnet", "expected"),
        [
            (True, True, "paper_testnet"),
            (False, True, "live_testnet"),
            (True, False, "paper_mainnet"),
            (False, False, "live_mainnet"),
        ],
    )
    def test_four_modes(self, paper, testnet, expected):
        assert resolve_mode(paper_trading=paper, binance_testnet=testnet) == expected

    def test_every_mode_has_meta(self):
        for mode in ("paper_testnet", "live_testnet", "paper_mainnet", "live_mainnet"):
            assert mode in MODE_META
            assert MODE_META[mode]["label"]
            assert MODE_META[mode]["detail"]

    def test_live_mainnet_is_danger_toned(self):
        assert MODE_META["live_mainnet"]["tone"] == "danger"


# ---------------------------------------------------------------------------
# 交易许可透传(不得重新判定)
# ---------------------------------------------------------------------------


class TestPermissionPassThrough:
    def test_can_buy_can_sell_come_from_health(self):
        st = build_operator_status(_settings(), _health(can_buy=False, can_sell=True,
                                                       buy_block_reason="行情数据不健康"))
        assert st["can_buy"] is False
        assert st["can_sell"] is True
        assert st["buy_block_reason"] == "行情数据不健康"

    def test_never_invents_permission_when_gate_missing(self):
        """交易闸门未就绪(空 health): 必须 fail-closed, 绝不放行。"""
        st = build_operator_status(_settings(), {})
        assert st["can_buy"] is False
        assert st["can_sell"] is False
        assert st["write_actions_enabled"] is False or isinstance(st["write_actions_enabled"], bool)

    def test_empty_health_does_not_raise(self):
        st = build_operator_status(_settings(), {"can_buy": None, "can_sell": None})
        assert st["can_buy"] is False and st["can_sell"] is False
        assert st["summary"]


# ---------------------------------------------------------------------------
# 状态 -> summary / risk_level / next_action
# ---------------------------------------------------------------------------


class TestStatusPresentation:
    @pytest.mark.parametrize(
        ("status", "risk_level"),
        [
            ("TRADING", "safe"),
            ("SAFE", "warning"),
            ("DEGRADED", "warning"),
            ("REDUCE_ONLY", "warning"),
            ("PAUSED", "danger"),
            ("RECOVERY", "danger"),
            ("KILLED", "blocked"),
        ],
    )
    def test_risk_level_mapping(self, status, risk_level):
        st = build_operator_status(_settings(), _health(status=status))
        assert st["risk_level"] == risk_level
        assert st["status"] == status

    def test_killed_summary_and_action(self):
        st = build_operator_status(
            _settings(),
            _health(status="KILLED", can_buy=False, can_sell=False,
                    buy_block_reason="急停中", sell_block_reason="急停中"),
        )
        assert st["risk_level"] == "blocked"
        assert "急停" in st["status_label"]
        assert "禁止买入" in st["summary"]
        # 急停冻结时买卖两侧都禁, 摘要必须同时讲清楚
        assert "禁止卖出" in st["summary"]
        # 必须点明「重启不会自动恢复」这一关键运维事实
        assert "重启" in st["next_action"]

    def test_reduce_only_says_sell_only(self):
        st = build_operator_status(
            _settings(),
            _health(status="REDUCE_ONLY", can_buy=False, can_sell=True,
                    buy_block_reason="仅减仓"),
        )
        assert st["risk_level"] == "warning"
        assert "禁止买入" in st["summary"]
        assert "允许卖出" in st["summary"]
        assert "只减仓" in st["next_action"]

    def test_paused(self):
        st = build_operator_status(
            _settings(),
            _health(status="PAUSED", can_buy=False, can_sell=False, buy_block_reason="风险暂停"),
        )
        assert st["risk_level"] == "danger"
        assert "冷却" in st["next_action"]

    def test_recovery(self):
        st = build_operator_status(
            _settings(),
            _health(status="RECOVERY", can_buy=False, can_sell=False, buy_block_reason="恢复核验中"),
        )
        assert st["risk_level"] == "danger"
        assert "对账" in st["next_action"]

    def test_trading_ok(self):
        st = build_operator_status(_settings(), _health())
        assert st["risk_level"] == "safe"
        assert "允许买入" in st["summary"]
        assert "允许卖出" in st["summary"]

    def test_unknown_status_falls_back(self):
        st = build_operator_status(_settings(), _health(status=""))
        assert st["status"] == "SAFE"
        assert st["status_label"]  # 不出现空标题

    def test_summary_and_action_are_human_readable(self):
        """主要提示不得直接裸露内部字段名。"""
        st = build_operator_status(
            _settings(), _health(status="KILLED", can_buy=False, can_sell=False,
                                 buy_block_reason="急停中")
        )
        for text in (st["summary"], st["next_action"]):
            for leaked in ("can_buy", "kill_switch", "buy_block_reason", "reconcile_drift_pct"):
                assert leaked not in text

    def test_suggest_next_action_generic_fallback(self):
        assert suggest_next_action("SAFE", "") == suggest_next_action("SAFE", "")


# ---------------------------------------------------------------------------
# 写操作开关与配置解释
# ---------------------------------------------------------------------------


class TestSwitches:
    def test_write_actions_disabled_without_token(self):
        st = build_operator_status(_settings(web_admin_token=""), _health())
        assert st["write_actions_enabled"] is False

    def test_write_actions_enabled_with_token(self):
        st = build_operator_status(_settings(web_admin_token="s3cret"), _health())
        assert st["write_actions_enabled"] is True

    def test_token_value_never_exposed(self):
        """WEB_ADMIN_TOKEN 只报配置状态, 绝不回显值。"""
        secret = "super-secret-token-value"
        st = build_operator_status(_settings(web_admin_token=secret), _health())
        blob = repr(st)
        assert secret not in blob
        switches = {s["key"]: s for s in st["switches"]}
        assert switches["WEB_ADMIN_TOKEN"]["value"] == "已配置"
        assert secret not in repr(switches)

    def test_switches_cover_required_keys(self):
        st = build_operator_status(_settings(), _health())
        keys = {s["key"] for s in st["switches"]}
        assert keys == {
            "PAPER_TRADING",
            "BINANCE_TESTNET",
            "LIVE_TRADING_CONFIRM",
            "MAINNET_API_SCOPE_CONFIRM",
            "WEB_ADMIN_TOKEN",
            "WEB_ADMIN_AUTH",
        }

    def test_paper_mode_explains_no_real_money(self):
        sw = {s["key"]: s for s in explain_switches(
            paper_trading=True, binance_testnet=True, live_trading_confirm="",
            mainnet_api_scope_confirmed=False, web_admin_token_configured=True,
        )}
        assert "不使用真实资金" in sw["PAPER_TRADING"]["text"]

    def test_mainnet_switch_warns(self):
        sw = {s["key"]: s for s in explain_switches(
            paper_trading=False, binance_testnet=False, live_trading_confirm="true",
            mainnet_api_scope_confirmed=True, web_admin_token_configured=True,
        )}
        assert sw["BINANCE_TESTNET"]["tone"] == "danger"
        assert "主网" in sw["BINANCE_TESTNET"]["text"]

    def test_dangerous_actions_exclude_kill(self):
        """急停是安全方向动作, 不应被列入需二次确认的危险操作。"""
        assert "emergency_kill" not in DANGEROUS_ACTIONS
        assert "emergency_recover" in DANGEROUS_ACTIONS

    # ---- V12.4: 写接口鉴权关闭时必须在界面上醒目暴露 ----

    def test_auth_disabled_flags_and_notice(self):
        st = build_operator_status(_settings(admin_auth_disabled=True), _health())
        assert st["auth_disabled"] is True
        assert "鉴权已关闭" in st["auth_notice"]
        sw = {s["key"]: s for s in st["switches"]}
        assert sw["WEB_ADMIN_AUTH"]["value"] == "已关闭"
        assert sw["WEB_ADMIN_AUTH"]["tone"] == "danger"
        assert "任何设备" in sw["WEB_ADMIN_AUTH"]["text"]

    def test_auth_enabled_by_default_no_notice(self):
        st = build_operator_status(_settings(admin_auth_disabled=False), _health())
        assert st["auth_disabled"] is False
        assert st["auth_notice"] == ""
        sw = {s["key"]: s for s in st["switches"]}
        assert sw["WEB_ADMIN_AUTH"]["value"] == "on"
        assert sw["WEB_ADMIN_AUTH"]["tone"] == "safe"

    def test_auth_disabled_makes_write_actions_available(self):
        """鉴权关闭 → 写操作可用(即使没配令牌)。"""
        st = build_operator_status(_settings(web_admin_token="", admin_auth_disabled=True),
                                   _health())
        assert st["write_actions_enabled"] is True

    def test_no_token_no_auth_disabled_means_writes_off(self):
        st = build_operator_status(_settings(web_admin_token="", admin_auth_disabled=False),
                                   _health())
        assert st["write_actions_enabled"] is False


# ---------------------------------------------------------------------------
# deploy / runtime 透传
# ---------------------------------------------------------------------------


class TestDeployBlock:
    def test_deploy_fields_present(self):
        st = build_operator_status(
            _settings(git_sha="abc123", image_tag="abc123"), _health()
        )
        assert st["deploy"]["git_sha"] == "abc123"
        assert st["deploy"]["image_tag"] == "abc123"
        assert st["deploy"]["app"] == "adaptiveTrading"

    def test_runtime_block_reads_last_error_and_reconcile(self):
        st = build_operator_status(
            _settings(),
            _health(last_error={"source": "web", "message": "boom", "ts": 1.0}),
        )
        assert st["runtime"]["last_error"]["message"] == "boom"
        assert st["runtime"]["last_reconcile_at"] == 1789132733.0

    def test_missing_reconcile_block_is_safe(self):
        st = build_operator_status(_settings(), {"can_buy": True, "can_sell": True})
        assert st["runtime"]["last_reconcile_at"] is None
        assert st["runtime"]["tasks_failed"] == 0
