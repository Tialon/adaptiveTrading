"""V11.3 P0-2 冻结单币 SOLUSDT 产品定义一致性。

冻结产品: Binance 单所 / SOLUSDT 单币 / 现货 / 双仓 / 低频。
本测试钉住「默认 symbol == SOLUSDT」+「非 SOLUSDT fail-fast(不静默支持多币种)」,
防止符号定义回归到 BTCUSDT 或偷偷扩成多币种。
"""

from at01_common.settings import SUPPORTED_SYMBOLS, Settings


def _cfg(**kw) -> Settings:
    """构造确定性配置(不读 .env; 测试 env 由 conftest 固定为 SOLUSDT)。"""
    return Settings(_env_file=None, **kw)


def test_default_symbol_is_solusdt():
    """默认标的必须为 SOLUSDT(不依赖环境变量, 直接钉住字段默认值)。"""
    assert Settings.model_fields["symbols"].default == "SOLUSDT"


def test_default_config_passes_validation():
    """默认配置(纸面 + 测试网 + SOLUSDT)必须整体通过 validate()。"""
    assert _cfg().validate() == []


def test_solusdt_passes_validation():
    """显式 SOLUSDT 不产生「冻结单币」问题。"""
    problems = _cfg(symbols="SOLUSDT").validate()
    assert not any("冻结单币" in p for p in problems)


def test_unsupported_symbol_fails_fast():
    """非 SOLUSDT(如 BTCUSDT)必须被 fail-fast 拦截。"""
    problems = _cfg(symbols="BTCUSDT").validate()
    assert any("冻结单币" in p and "BTCUSDT" in p for p in problems)


def test_multi_symbol_fails_fast():
    """混入多币种(SOLUSDT,BTCUSDT)同样 fail-fast, 不静默支持多币种。"""
    problems = _cfg(symbols="SOLUSDT,BTCUSDT").validate()
    assert any("冻结单币" in p and "BTCUSDT" in p for p in problems)


def test_empty_symbols_blocked():
    """空标的仍被拒绝(与冻结单币检查互斥, 空只报「SYMBOLS 为空」)。"""
    problems = _cfg(symbols=" ,, ").validate()
    assert any("SYMBOLS 为空" in p for p in problems)
    assert not any("冻结单币" in p for p in problems)


def test_supported_symbols_is_single_coin():
    """支持列表恒为单币 SOLUSDT(冻结不变)。"""
    assert SUPPORTED_SYMBOLS == ("SOLUSDT",)
