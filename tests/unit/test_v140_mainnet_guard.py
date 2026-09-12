"""V11.3 P1-4 Production Safety Audit —— 主网守卫。

对 `Settings.mainnet_blocked_reason()` 钉死**真钱交易**的门槛:
- 默认(测试网 / 纸面)绝不拦截;
- **`PAPER_TRADING=false` + `BINANCE_TESTNET=false`(主网真实)未显式确认 → 一律拒绝**;
- 显式确认(大小写 / 空白容忍)后放行。

**V12.6 变更**: 判定条件由「是否连主网」改为「**是否可能用真钱下单**」——
`PAPER_TRADING=true` + `BINANCE_TESTNET=false`(主网观察)现在放行。
理由: 该模式没有任何真钱能力(下单走 PaperBroker; 三个用 REST 的对账器都是
`rest_client=None`; `validate()` 也不要求主网凭证), 拦它是挂错条件的重复守卫。
而「从主网观察改成主网真实」需要 `PAPER_TRADING=false`, 那会再次进入本函数且
**仍会被拦** —— 所以放宽这里并未打开任何意外真钱交易的路径。

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
    """主网**真实**未确认 → 拦。注意须显式 `paper_trading=False`。

    V12.6 前这里靠 `_cfg` 的默认 `paper_trading=True` 走的是「主网观察」路径;
    现在那条放行了, 所以必须显式声明"要真钱交易"才测得到这道守卫。
    """
    reason = _cfg(paper_trading=False, binance_testnet=False,
                  live_trading_confirm="").mainnet_blocked_reason()
    assert reason is not None
    assert "LIVE_TRADING_CONFIRM" in reason


def test_paper_mainnet_allowed_without_confirm():
    """V12.6: 主网观察(纸面 + 主网行情)**放行** —— 它没有真钱能力。

    这是本次**有意放宽**的一条, 但不是放宽安全: 真钱门槛仍在
    (`test_mainnet_without_confirm_blocked` / `test_live_mainnet_blocked_without_confirm`),
    且从主网观察切到主网真实必须过那道门槛。
    """
    reason = _cfg(paper_trading=True, binance_testnet=False,
                  live_trading_confirm="").mainnet_blocked_reason()
    assert reason is None


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
    # 非 "true" 的显式值(如 "yes"/"1")仍拒绝 —— 只认 "true"(真钱路径)
    assert _cfg(paper_trading=False, binance_testnet=False,
                live_trading_confirm="yes").mainnet_blocked_reason() is not None
