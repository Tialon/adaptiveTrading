"""V11.3 P0-3 Settings 全量 Fail-Fast 审计。

对 `Settings.validate()` 的新增数值/语义校验做回归, 覆盖风控阈值、网格、
止盈阶梯、均线周期、买入阈值、端口、策略开关、DB 地址等字段。
全部用默认(合法)配置做基线, 仅翻转单个字段断言其被 fail-fast 拦截。
"""

import pytest

from at01_common.settings import Settings


def _cfg(**kw) -> Settings:
    """构造确定性配置(不读 .env)。"""
    return Settings(_env_file=None, **kw)


# ---------------------------------------------------------------------------
# 基线
# ---------------------------------------------------------------------------

def test_default_config_passes():
    """默认配置(纸面 + 测试网 + SOLUSDT)必须整体通过, 不产生任何问题。"""
    assert _cfg().validate() == []


# ---------------------------------------------------------------------------
# 风控百分比阈值(0, 1]
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "field",
    ["risk_max_position_pct", "risk_max_single_order_pct", "risk_max_daily_loss", "risk_max_drawdown"],
)
def test_risk_pct_out_of_range_blocked(field):
    for bad in (0.0, -0.01, 1.01):
        problems = _cfg(**{field: bad}).validate()
        assert any(field in p for p in problems), f"{field}={bad} 应被拦截"


def test_risk_pct_boundaries_pass():
    # 端点值 (0,1] 合法: 上限 1.0 允许, 0.0 以下拒绝
    assert _cfg(risk_max_position_pct=1.0).validate() == []
    assert _cfg(risk_max_single_order_pct=0.05).validate() == []


# ---------------------------------------------------------------------------
# 初始权益 / 手续费率
# ---------------------------------------------------------------------------

def test_initial_equity_non_positive_blocked():
    problems = _cfg(risk_initial_equity=0).validate()
    assert any("risk_initial_equity" in p for p in problems)


def test_paper_fee_rate_out_of_range_blocked():
    problems = _cfg(paper_fee_rate=1.5).validate()
    assert any("paper_fee_rate" in p for p in problems)
    # 负数也应被拦
    problems = _cfg(paper_fee_rate=-0.01).validate()
    assert any("paper_fee_rate" in p for p in problems)


# ---------------------------------------------------------------------------
# 网格
# ---------------------------------------------------------------------------

def test_grid_count_too_small_blocked():
    problems = _cfg(grid_count=1).validate()
    assert any("grid_count" in p for p in problems)


def test_grid_boundary_pct_non_positive_blocked():
    problems = _cfg(grid_upper_pct=0.0).validate()
    assert any("网格" in p for p in problems)
    problems = _cfg(grid_lower_pct=-0.01).validate()
    assert any("网格" in p for p in problems)


# ---------------------------------------------------------------------------
# 止盈阶梯
# ---------------------------------------------------------------------------

def test_take_profit_ladder_unparseable_blocked():
    for bad in ("", "::::", "abc", "5:", ":20", "5:0", "5:150"):
        problems = _cfg(sell_take_profit_ladder=bad).validate()
        assert any("sell_take_profit_ladder" in p for p in problems), f"ladder={bad!r} 应被拦截"


def test_take_profit_ladder_valid_passes():
    assert _cfg(sell_take_profit_ladder="5:20,10:30,20:50").validate() == []


# ---------------------------------------------------------------------------
# 执行 / 周期
# ---------------------------------------------------------------------------

def test_execution_max_retry_negative_blocked():
    problems = _cfg(execution_max_retry=-1).validate()
    assert any("execution_max_retry" in p for p in problems)


def test_reconcile_interval_non_positive_blocked():
    problems = _cfg(reconcile_interval_seconds=0).validate()
    assert any("reconcile_interval_seconds" in p for p in problems)


def test_rebalance_interval_non_positive_blocked():
    problems = _cfg(portfolio_rebalance_interval_seconds=-5).validate()
    assert any("portfolio_rebalance_interval_seconds" in p for p in problems)


# ---------------------------------------------------------------------------
# 均线周期 / 买入阈值
# ---------------------------------------------------------------------------

def test_trend_period_fast_not_less_than_slow_blocked():
    problems = _cfg(trend_fast_period=26, trend_slow_period=12).validate()
    assert any("fast" in p and "slow" in p for p in problems)


def test_entry_threshold_order_blocked():
    problems = _cfg(entry_observe_threshold=90, entry_buy_threshold=80).validate()
    assert any("observe" in p and "buy" in p for p in problems)
    # buy 超过 100 非法
    problems = _cfg(entry_buy_threshold=101).validate()
    assert any("买入阈值" in p for p in problems)


# ---------------------------------------------------------------------------
# 端口 / 策略开关 / DB
# ---------------------------------------------------------------------------

def test_api_port_out_of_range_blocked():
    problems = _cfg(api_port=70000).validate()
    assert any("api_port" in p for p in problems)
    problems = _cfg(api_port=0).validate()
    assert any("api_port" in p for p in problems)


def test_no_strategy_enabled_blocked():
    problems = _cfg(strategy_enabled=" ,, ").validate()
    assert any("strategy_enabled" in p for p in problems)


def test_empty_database_url_blocked():
    problems = _cfg(database_url="").validate()
    assert any("database_url" in p for p in problems)
