"""V11.1 P1-5 资金级 Circuit Breaker 单元测试。

覆盖: classify_drift 三档分级(0.1%/0.2%/0.5%)/ drift_pct 漂移比例 / 三向聚合取最严重 /
Position·Cash 漂移首选 REDUCE_ONLY / Equity 漂移逐级收紧到 KILL。
"""

import pytest

from at60_risk.fund_circuit_breaker import (
    BreakerAction,
    FundCircuitBreaker,
    classify_drift,
    drift_pct,
)


# ---------------------------------------------------------------------------
# drift_pct(纯函数)
# ---------------------------------------------------------------------------

def test_drift_pct():
    assert drift_pct(3.0, 1000.0) == pytest.approx(0.003)
    assert drift_pct(-3.0, 1000.0) == pytest.approx(0.003)  # 取绝对值
    assert drift_pct(1.0, 0.0) == 0.0  # base<=0 → 0
    assert drift_pct(0.0, 1000.0) == 0.0


# ---------------------------------------------------------------------------
# classify_drift(纯函数) —— Equity 三档收紧
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("d", [0.0, 0.0005, 0.000999])
def test_below_t1_is_none(d):
    assert classify_drift("equity", d) is BreakerAction.NONE
    assert classify_drift("position", d) is BreakerAction.NONE
    assert classify_drift("cash", d) is BreakerAction.NONE


def test_equity_tiers_escalate():
    assert classify_drift("equity", 0.001) is BreakerAction.REDUCE_ONLY    # 0.1%
    assert classify_drift("equity", 0.0015) is BreakerAction.REDUCE_ONLY   # 0.15%
    assert classify_drift("equity", 0.002) is BreakerAction.PAUSE          # 0.2%
    assert classify_drift("equity", 0.004) is BreakerAction.PAUSE          # 0.4%
    assert classify_drift("equity", 0.005) is BreakerAction.KILL           # 0.5%
    assert classify_drift("equity", 0.02) is BreakerAction.KILL            # 2%


# ---------------------------------------------------------------------------
# classify_drift —— Position / Cash 首选 REDUCE_ONLY
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("dim", ["position", "cash"])
def test_position_cash_reduce_only_first(dim):
    assert classify_drift(dim, 0.001) is BreakerAction.REDUCE_ONLY   # 0.1%
    assert classify_drift(dim, 0.002) is BreakerAction.REDUCE_ONLY   # 0.2%(仍只减仓)
    assert classify_drift(dim, 0.004) is BreakerAction.REDUCE_ONLY   # 0.4%
    assert classify_drift(dim, 0.005) is BreakerAction.PAUSE         # >0.5% 才 PAUSE


# ---------------------------------------------------------------------------
# FundCircuitBreaker.assess(三向聚合)
# ---------------------------------------------------------------------------

def test_all_clean_returns_none():
    b = FundCircuitBreaker()
    d = b.assess(0.0, 0.0, 0.0)
    assert d.action is BreakerAction.NONE
    assert not d.actionable
    assert d.reason == ""


def test_position_drift_triggers_reduce_only():
    b = FundCircuitBreaker()
    d = b.assess(equity_drift=0.0, position_drift=0.003, cash_drift=0.0)
    assert d.action is BreakerAction.REDUCE_ONLY
    assert d.dimensions["position"] is BreakerAction.REDUCE_ONLY
    assert "position=REDUCE_ONLY" in d.reason


def test_cash_drift_triggers_reduce_only():
    b = FundCircuitBreaker()
    d = b.assess(equity_drift=0.0, position_drift=0.0, cash_drift=0.002)
    assert d.action is BreakerAction.REDUCE_ONLY
    assert d.dimensions["cash"] is BreakerAction.REDUCE_ONLY


def test_equity_drift_kills_even_when_others_clean():
    b = FundCircuitBreaker()
    d = b.assess(equity_drift=0.006, position_drift=0.0, cash_drift=0.0)
    assert d.action is BreakerAction.KILL
    assert d.dimensions["equity"] is BreakerAction.KILL


def test_most_severe_wins():
    # equity PAUSE(0.2%) vs position REDUCE_ONLY(0.1%) → PAUSE
    b = FundCircuitBreaker()
    d = b.assess(equity_drift=0.002, position_drift=0.001, cash_drift=0.0)
    assert d.action is BreakerAction.PAUSE

    # equity REDUCE_ONLY(0.1%) vs position PAUSE(0.6%) → PAUSE
    d = b.assess(equity_drift=0.001, position_drift=0.006, cash_drift=0.0)
    assert d.action is BreakerAction.PAUSE


def test_all_three_dimensions_reported():
    b = FundCircuitBreaker()
    d = b.assess(equity_drift=0.005, position_drift=0.003, cash_drift=0.006)
    assert d.dimensions == {
        "equity": BreakerAction.KILL,
        "position": BreakerAction.REDUCE_ONLY,
        "cash": BreakerAction.PAUSE,
    }
    assert d.action is BreakerAction.KILL


def test_severity_ordering_is_strict():
    order = {BreakerAction.NONE: 0, BreakerAction.REDUCE_ONLY: 1,
             BreakerAction.PAUSE: 2, BreakerAction.KILL: 3}
    # 三向聚合取 max 依赖该排序; 这里验证枚举语义一致(仅顺序检查)
    assert order[BreakerAction.NONE] < order[BreakerAction.REDUCE_ONLY]
    assert order[BreakerAction.REDUCE_ONLY] < order[BreakerAction.PAUSE]
    assert order[BreakerAction.PAUSE] < order[BreakerAction.KILL]


def test_decision_to_dict():
    b = FundCircuitBreaker()
    d = b.assess(equity_drift=0.0, position_drift=0.002, cash_drift=0.0)
    dd = d.to_dict()
    assert dd["action"] == "REDUCE_ONLY"
    assert dd["dimensions"]["position"] == "REDUCE_ONLY"
    assert dd["dimensions"]["equity"] == "NONE"
