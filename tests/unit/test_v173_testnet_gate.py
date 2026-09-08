"""V11.7 P1-4 证明: 测试网真实执行闸门(testnet_preflight / format_preflight_report / git_sha)。

覆盖 `at01_common/testnet_gate.py`:
- 纸面模式: 无条件 allowed(mode="paper"), 不触发真实执行闸门;
- 非纸面: 四项硬条件(BINANCE_TESTNET=true / RUN_TESTNET_TRADING=1 /
  live_trading=false / credentials_present)全满足才 allowed;
- 任一不满足 → BLOCKED(blocked_reasons 明确);
- 报告块格式含 symbol/paper/binance_testnet/live_trading/git_sha/credentials_present,
  blocked 时追加 BLOCKED 行;
- git_sha 真实返回 40 hex(不硬编码)。
"""

import re

from at01_common.testnet_gate import (
    format_preflight_report,
    git_sha,
    testnet_preflight as preflight,
)


def _pf(**overrides):
    base = {
        "binance_testnet": True,
        "paper_trading": False,
        "live_trading": False,
        "run_testnet_trading": "1",
        "credentials_present": True,
        "git_sha": "a" * 40,
        "symbol": "SOLUSDT",
    }
    base.update(overrides)
    return preflight(**base)


def test_real_testnet_all_conditions_met():
    r = _pf()
    assert r["allowed"] is True
    assert r["mode"] == "real_testnet"
    assert r["blocked_reasons"] == []
    assert r["report"]["paper_trading"] is False
    assert r["report"]["binance_testnet"] is True
    assert r["report"]["live_trading"] is False
    assert r["report"]["credentials_present"] is True


def test_paper_mode_always_allowed():
    """纸面模式无条件 allowed, 即便其它硬条件全不满足(本就无真实下单)。"""
    r = _pf(
        paper_trading=True,
        binance_testnet=False,
        run_testnet_trading="",
        credentials_present=False,
    )
    assert r["allowed"] is True
    assert r["mode"] == "paper"
    assert r["blocked_reasons"] == []


def test_blocked_missing_run_testnet_trading():
    r = _pf(run_testnet_trading="")
    assert r["allowed"] is False
    assert any("RUN_TESTNET_TRADING" in x for x in r["blocked_reasons"])


def test_blocked_mainnet_binance_testnet_false():
    r = _pf(binance_testnet=False)
    assert r["allowed"] is False
    assert any("主网" in x for x in r["blocked_reasons"])


def test_blocked_live_trading_true():
    r = _pf(live_trading=True)
    assert r["allowed"] is False
    assert any("live_trading=true" in x for x in r["blocked_reasons"])


def test_blocked_missing_credentials():
    r = _pf(credentials_present=False)
    assert r["allowed"] is False
    assert any("key/secret" in x for x in r["blocked_reasons"])


def test_multiple_blocked_reasons_accumulate():
    r = _pf(binance_testnet=False, run_testnet_trading="", credentials_present=False)
    assert r["allowed"] is False
    assert len(r["blocked_reasons"]) == 3


# ---------------------------------------------------------------------------
# 报告格式
# ---------------------------------------------------------------------------


def test_report_format_allowed():
    r = _pf()
    text = format_preflight_report(r)
    assert "=== TESTNET PREFLIGHT ===" in text
    assert "symbol=SOLUSDT" in text
    assert "paper_trading=false" in text
    assert "binance_testnet=true" in text
    assert "live_trading=false" in text
    assert "credentials_present=true" in text
    assert "BLOCKED" not in text


def test_report_format_blocked():
    r = _pf(run_testnet_trading="")
    text = format_preflight_report(r)
    assert "BLOCKED:" in text
    assert "RUN_TESTNET_TRADING" in text


# ---------------------------------------------------------------------------
# git_sha
# ---------------------------------------------------------------------------


def test_git_sha_real_repo():
    """本仓库是 git 仓库, git_sha() 应返回 40 位 hex(证明不硬编码、cwd 正确)。"""
    sha = git_sha()
    assert re.match(r"^[0-9a-f]{40}$", sha), f"git_sha() = {sha!r}"
