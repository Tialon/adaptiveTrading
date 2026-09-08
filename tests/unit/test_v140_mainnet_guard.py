"""V11.3 P1-4 Production Safety Audit —— 默认禁主网守卫。

对 `Settings.mainnet_blocked_reason()` 钉死「默认禁主网」不变量:
- 默认(测试网 / 纸面)绝不拦截;
- 只要 `BINANCE_TESTNET=false`(连主网), 未显式 `LIVE_TRADING_CONFIRM=true` 一律拒绝 ——
  即便 PAPER_TRADING=true(纯主网行情源)也不例外;
- 显式确认(大小写 / 空白容忍)后放行。

冻结不变: 纯生产安全守卫, 不新增策略/币种/合约。
"""

from at01_common.settings import Settings


def _cfg(**kw) -> Settings:
    """构造确定性配置(不读 .env)。"""
    return Settings(_env_file=None, **kw)


# ---------------------------------------------------------------------------
# 默认安全(绝不拦截)
# ---------------------------------------------------------------------------

def test_default_testnet_not_blocked():
    assert _cfg(binance_testnet=True, live_trading_confirm="").mainnet_blocked_reason() is None


def test_default_paper_testnet_not_blocked():
    assert _cfg(paper_trading=True, binance_testnet=True).mainnet_blocked_reason() is None


# ---------------------------------------------------------------------------
# 主网默认拦截
# ---------------------------------------------------------------------------

def test_mainnet_without_confirm_blocked():
    reason = _cfg(binance_testnet=False, live_trading_confirm="").mainnet_blocked_reason()
    assert reason is not None
    assert "LIVE_TRADING_CONFIRM" in reason


def test_paper_mainnet_still_blocked_without_confirm():
    # 关键不变量: 即便纸面(PAPER_TRADING=true), 连主网也需显式确认
    reason = _cfg(paper_trading=True, binance_testnet=False, live_trading_confirm="").mainnet_blocked_reason()
    assert reason is not None


def test_live_mainnet_blocked_without_confirm():
    reason = _cfg(paper_trading=False, binance_testnet=False, live_trading_confirm="").mainnet_blocked_reason()
    assert reason is not None


# ---------------------------------------------------------------------------
# 显式确认放行
# ---------------------------------------------------------------------------

def test_mainnet_with_confirm_allowed():
    assert _cfg(binance_testnet=False, live_trading_confirm="true").mainnet_blocked_reason() is None


def test_mainnet_confirm_case_insensitive():
    assert _cfg(binance_testnet=False, live_trading_confirm="TRUE").mainnet_blocked_reason() is None


def test_mainnet_confirm_whitespace_tolerated():
    assert _cfg(binance_testnet=False, live_trading_confirm=" true ").mainnet_blocked_reason() is None


def test_mainnet_confirm_other_value_still_blocked():
    # 非 "true" 的显式值(如 "yes"/"1")仍拒绝 —— 只认 "true"
    assert _cfg(binance_testnet=False, live_trading_confirm="yes").mainnet_blocked_reason() is not None
