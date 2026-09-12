"""V11.2 P0-4 资金熔断执行链单元测试。

覆盖「交易所真相 → 漂移计算(compute_drift)→ FundCircuitBreaker.assess → BreakerDecision」
的端到端纯链路正确性; 以及 truth_incomplete / missing 时绝不产生错误的断路器动作
(该路径交由对账矩阵 DEGRADED 处置, 而非据此误判 KILL)。
"""


from at60_execution.drift import compute_drift
from at50_risk.fund_circuit_breaker import BreakerAction, BreakerDecision, FundCircuitBreaker


def _assess_drift(**kwargs) -> BreakerDecision:
    """模拟 run.py 执行链: drift -> assess(仅在 trusted 时)。"""
    drift = compute_drift(**kwargs)
    assert drift.trusted
    return FundCircuitBreaker().assess(
        equity_drift=drift.equity_drift,
        position_drift=drift.position_drift,
        cash_drift=drift.cash_drift,
    )


# ---------------------------------------------------------------------------
# 一致账本(本地=交易所)→ 全零漂移 → NONE
# ---------------------------------------------------------------------------

def test_consistent_ledger_is_none():
    # equity = cash + position*price 两边自洽, 无任何漂移
    price = 100.0
    local_cash, local_position = 5000.0, 5.0
    equity = local_cash + local_position * price  # 5500
    d = _assess_drift(
        local_equity=equity, exchange_equity=equity,
        local_position=local_position, exchange_position=local_position,
        local_cash=local_cash, exchange_cash=local_cash,
    )
    assert d.action is BreakerAction.NONE


# ---------------------------------------------------------------------------
# 权益漂移(资金级最严重, 逐级收紧)
# ---------------------------------------------------------------------------

def test_equity_drift_escalates_to_kill():
    d = _assess_drift(
        local_equity=10000.0, exchange_equity=10070.0,  # 0.7%
        local_position=0.0, exchange_position=0.0,
        local_cash=10000.0, exchange_cash=10070.0,
    )
    assert d.action is BreakerAction.KILL


def test_equity_drift_pauses():
    d = _assess_drift(
        local_equity=10000.0, exchange_equity=10030.0,  # 0.3%
        local_position=0.0, exchange_position=0.0,
        local_cash=10000.0, exchange_cash=10030.0,
    )
    assert d.action is BreakerAction.PAUSE


def test_equity_drift_reduce_only():
    d = _assess_drift(
        local_equity=10000.0, exchange_equity=10015.0,  # 0.15%
        local_position=0.0, exchange_position=0.0,
        local_cash=10000.0, exchange_cash=10015.0,
    )
    assert d.action is BreakerAction.REDUCE_ONLY


# ---------------------------------------------------------------------------
# 持仓漂移
# ---------------------------------------------------------------------------

def test_position_drift_reduce_only():
    d = _assess_drift(
        local_equity=10000.0, exchange_equity=10000.0,
        local_position=100.0, exchange_position=99.8,  # 0.2% 持仓漂移
        local_cash=5000.0, exchange_cash=5000.0,
    )
    assert d.action is BreakerAction.REDUCE_ONLY


def test_position_drift_pauses():
    d = _assess_drift(
        local_equity=10000.0, exchange_equity=10000.0,
        local_position=100.0, exchange_position=99.4,  # 0.6% 持仓漂移
        local_cash=5000.0, exchange_cash=5000.0,
    )
    assert d.action is BreakerAction.PAUSE


# ---------------------------------------------------------------------------
# 现金漂移
# ---------------------------------------------------------------------------

def test_cash_drift_reduce_only():
    d = _assess_drift(
        local_equity=5000.0, exchange_equity=5000.0,
        local_position=0.0, exchange_position=0.0,
        local_cash=5000.0, exchange_cash=4990.0,  # 0.2% 现金漂移
    )
    assert d.action is BreakerAction.REDUCE_ONLY


# ---------------------------------------------------------------------------
# 真相不完整 → 不可信 → 跳过断路器(绝不误判 KILL)
# ---------------------------------------------------------------------------

def test_truth_incomplete_never_triggers_breaker():
    drift = compute_drift(
        local_equity=10000.0, exchange_equity=10070.0,  # 看似 0.7% 权益漂移
        local_position=0.0, exchange_position=0.0,
        local_cash=10000.0, exchange_cash=10070.0,
        truth_complete=False,
    )
    assert not drift.trusted
    assert drift.equity_drift is None
    # run.py 上层逻辑: 不可信 → 决策 NONE(交矩阵 DEGRADED 处置)
    decision = FundCircuitBreaker().assess()
    assert decision.action is BreakerAction.NONE


def test_missing_equity_never_triggers_breaker():
    drift = compute_drift(
        local_equity=None, exchange_equity=10070.0,
        local_position=0.0, exchange_position=0.0,
        local_cash=10000.0, exchange_cash=10070.0,
    )
    assert not drift.trusted
    assert drift.reason == "missing equity"


def test_local_equity_non_positive_never_triggers_breaker():
    drift = compute_drift(
        local_equity=0.0, exchange_equity=0.0,
        local_position=5.0, exchange_position=5.0,
        local_cash=500.0, exchange_cash=500.0,
    )
    assert not drift.trusted
    assert drift.reason == "local_equity <= 0"
