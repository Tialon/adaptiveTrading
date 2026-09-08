"""V11.8 §21 证明: 主网就绪自检(mainnet_readiness_check / format_readiness_report)。

覆盖 `at01_common/mainnet_readiness.py`:
- 全条件满足 → allowed(主网 + 真实 + 显式确认 + API 权限已确认 + SOLUSDT +
  配置通过 + 无急停 + git_sha 非空 + api.binance.com);
- 任一不满足 → BLOCKED(blocked_reasons 明确且逐项可定位);
- 多个阻断项累加;
- 报告块格式含 symbol/binance_testnet/paper_trading/live_trading_confirm/
  api_scope_confirmed/config_ok/kill_switch_armed/git_sha/base_url, blocked 追加 BLOCKED 行。

设计原则: 全为本地确定性判定(不查交易所), 纯函数可测; 默认禁主网由
`mainnet_blocked_reason()` + 本自检双层兜底, 绝不「带着未完成项静默进入主网」。
"""

from at01_common.mainnet_readiness import (
    format_readiness_report,
    mainnet_readiness_check as check,
)


def _c(**overrides):
    base = {
        "binance_testnet": False,
        "paper_trading": False,
        "live_trading_confirm": "true",
        "api_scope_confirmed": True,
        "symbol": "SOLUSDT",
        "config_problems": [],
        "kill_switch_armed": False,
        "git_sha": "a" * 40,
        "base_url": "https://api.binance.com",
    }
    base.update(overrides)
    return check(**base)


def test_mainnet_all_conditions_met():
    r = _c()
    assert r["allowed"] is True
    assert r["blocked_reasons"] == []
    assert r["report"]["symbol"] == "SOLUSDT"
    assert r["report"]["binance_testnet"] is False
    assert r["report"]["paper_trading"] is False
    assert r["report"]["live_trading_confirm"] is True
    assert r["report"]["api_scope_confirmed"] is True
    assert r["report"]["config_ok"] is True
    assert r["report"]["kill_switch_armed"] is False


def test_blocked_testnet_mode():
    """主网自检仅在 BINANCE_TESTNET=false 时进行; 若误传 true 应直接阻断。"""
    r = _c(binance_testnet=True)
    assert r["allowed"] is False
    assert any("binance_testnet=true" in x for x in r["blocked_reasons"])


def test_blocked_paper_trading():
    r = _c(paper_trading=True)
    assert r["allowed"] is False
    assert any("paper_trading=true" in x for x in r["blocked_reasons"])


def test_blocked_no_live_confirm():
    for bad in ("", "false", "0", "yes"):
        r = _c(live_trading_confirm=bad)
        assert r["allowed"] is False, f"live_trading_confirm={bad!r}"
        assert any("LIVE_TRADING_CONFIRM" in x for x in r["blocked_reasons"])


def test_live_confirm_case_and_whitespace_insensitive():
    """LIVE_TRADING_CONFIRM 大小写/首尾空白不敏感(与 mainnet_blocked_reason 语义一致)。"""
    r = _c(live_trading_confirm="  TRUE  ")
    assert r["allowed"] is True


def test_blocked_api_scope_not_confirmed():
    r = _c(api_scope_confirmed=False)
    assert r["allowed"] is False
    assert any("MAINNET_API_SCOPE_CONFIRM" in x for x in r["blocked_reasons"])


def test_blocked_unsupported_symbol():
    r = _c(symbol="BTCUSDT")
    assert r["allowed"] is False
    assert any("BTCUSDT" in x for x in r["blocked_reasons"])


def test_blocked_config_problems():
    r = _c(config_problems=["组合三桶比例和 1.3000 != 1.0"])
    assert r["allowed"] is False
    assert any("配置审计未通过" in x for x in r["blocked_reasons"])


def test_blocked_kill_switch_armed():
    r = _c(kill_switch_armed=True)
    assert r["allowed"] is False
    assert any("kill switch" in x for x in r["blocked_reasons"])


def test_blocked_empty_git_sha():
    r = _c(git_sha="")
    assert r["allowed"] is False
    assert any("git_sha" in x for x in r["blocked_reasons"])


def test_blocked_testnet_base_url():
    for bad in ("https://testnet.binance.vision", "https://api.example.com", ""):
        r = _c(base_url=bad)
        assert r["allowed"] is False, f"base_url={bad!r}"
        assert any("base_url" in x for x in r["blocked_reasons"])


def test_multiple_blocked_reasons_accumulate():
    r = _c(
        paper_trading=True,
        live_trading_confirm="",
        api_scope_confirmed=False,
        git_sha="",
    )
    assert r["allowed"] is False
    assert len(r["blocked_reasons"]) == 4


# ---------------------------------------------------------------------------
# 报告格式
# ---------------------------------------------------------------------------


def test_report_format_allowed():
    text = format_readiness_report(_c())
    assert "=== MAINNET READINESS ===" in text
    assert "symbol=SOLUSDT" in text
    assert "binance_testnet=false" in text
    assert "paper_trading=false" in text
    assert "live_trading_confirm=true" in text
    assert "api_scope_confirmed=true" in text
    assert "config_ok=true" in text
    assert "kill_switch_armed=false" in text
    assert "BLOCKED" not in text


def test_report_format_blocked():
    text = format_readiness_report(_c(api_scope_confirmed=False))
    assert "BLOCKED:" in text
    assert "MAINNET_API_SCOPE_CONFIRM" in text
