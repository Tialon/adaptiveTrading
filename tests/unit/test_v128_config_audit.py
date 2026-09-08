"""V11.2 P1-4 Production Configuration Audit。

对 `Settings.validate()` 做审计回归:
- 默认(纸面 + 测试网)配置必须通过;
- 实盘(PAPER_TRADING=false)缺对应环境 API key/secret 必须被拦截;
- 标的列表为空必须被拦截;
- 组合三桶比例(核心/交易/现金)和 != 1.0 必须被拦截。

注: 构造配置用 `_env_file=None` 屏蔽真实 .env(含真实密钥), 保证测试确定性且不触碰到密钥。
"""

from at01_common.settings import Settings


def _cfg(**kw) -> Settings:
    """构造确定性配置(不读 .env)。"""
    return Settings(_env_file=None, **kw)


# ---------------------------------------------------------------------------
# 通过场景
# ---------------------------------------------------------------------------

def test_default_paper_testnet_passes():
    # 默认纸面 + 测试网 + 无需 key -> 通过
    assert _cfg(paper_trading=True, binance_testnet=True).validate() == []


def test_paper_mainnet_passes_without_keys():
    # 纸面(不真实下单)即使用主网行情源, 也不需要下单 key
    assert _cfg(paper_trading=True, binance_testnet=False).validate() == []


def test_live_testnet_with_keys_passes():
    assert _cfg(
        paper_trading=False, binance_testnet=True,
        binance_testnet_api_key="k", binance_testnet_api_secret="s",
    ).validate() == []


def test_live_mainnet_with_keys_passes():
    assert _cfg(
        paper_trading=False, binance_testnet=False,
        binance_api_key="k", binance_api_secret="s",
    ).validate() == []


# ---------------------------------------------------------------------------
# 拦截场景
# ---------------------------------------------------------------------------

def test_live_testnet_missing_keys_blocked():
    problems = _cfg(paper_trading=False, binance_testnet=True).validate()
    assert any("测试网" in p and "KEY" in p for p in problems)


def test_live_mainnet_missing_keys_blocked():
    problems = _cfg(paper_trading=False, binance_testnet=False).validate()
    assert any("主网" in p and "KEY" in p for p in problems)


def test_empty_symbols_blocked():
    problems = _cfg(symbols=" ,, ").validate()
    assert any("SYMBOLS" in p for p in problems)


def test_bucket_ratio_sum_not_one_blocked():
    problems = _cfg(
        portfolio_core_ratio=0.5, portfolio_trading_ratio=0.4, portfolio_cash_ratio=0.4,
    ).validate()
    assert any("三桶比例" in p for p in problems)


def test_default_bucket_ratios_sum_to_one():
    s = _cfg()
    total = s.portfolio_core_ratio + s.portfolio_trading_ratio + s.portfolio_cash_ratio
    assert total == 1.0  # 默认比例自洽, validate 不因比例报错
    assert not any("三桶比例" in p for p in s.validate())
