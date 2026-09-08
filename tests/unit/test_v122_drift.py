"""V11.2 P0-3 资金漂移定义单元测试。

覆盖: 三向 numerator/denominator 语义 / 零基行为 / 单边失配 1.0 /
missing-data 绝不当 0 / truth_incomplete 整体不可信。
"""

import pytest

from at50_execution.drift import (
    DriftResult,
    compute_drift,
    equity_ratio,
    symmetric_ratio,
)


# ---------------------------------------------------------------------------
# 基础比例
# ---------------------------------------------------------------------------

def test_symmetric_ratio_zero_base():
    assert symmetric_ratio(0.0, 0.0) == 0.0  # 双边 0 → 0 漂移


def test_symmetric_ratio_one_sided():
    assert symmetric_ratio(0.0, 10.0) == 1.0  # 单边有 → 完整失配
    assert symmetric_ratio(10.0, 0.0) == 1.0


def test_symmetric_ratio_normal():
    assert symmetric_ratio(10.0, 12.0) == pytest.approx(2.0 / 12.0)
    assert symmetric_ratio(10.0, 10.0) == 0.0


def test_equity_ratio_non_positive_base_is_none():
    assert equity_ratio(0.0, 100.0) is None
    assert equity_ratio(-5.0, 100.0) is None


def test_equity_ratio_normal():
    assert equity_ratio(1000.0, 1003.0) == pytest.approx(3.0 / 1000.0)
    assert equity_ratio(1000.0, 1000.0) == 0.0


# ---------------------------------------------------------------------------
# compute_drift 可信路径
# ---------------------------------------------------------------------------

def test_compute_drift_all_zero():
    r = compute_drift(1000.0, 1000.0, 0.0, 0.0, 500.0, 500.0)
    assert r.trusted
    assert r.equity_drift == 0.0
    assert r.position_drift == 0.0
    assert r.cash_drift == 0.0


def test_compute_drift_position_mismatch():
    r = compute_drift(1000.0, 1000.0, 5.0, 3.0, 500.0, 500.0)
    assert r.trusted
    assert r.position_drift == pytest.approx(2.0 / 5.0)
    assert r.equity_drift == 0.0


def test_compute_drift_cash_one_sided():
    r = compute_drift(1000.0, 1000.0, 0.0, 0.0, 0.0, 200.0)
    assert r.trusted
    assert r.cash_drift == 1.0


# ---------------------------------------------------------------------------
# missing / truth_incomplete → 不可信(绝不 0 drift)
# ---------------------------------------------------------------------------

def test_truth_incomplete_untrusted():
    r = compute_drift(1000.0, 1000.0, 5.0, 5.0, 500.0, 500.0, truth_complete=False)
    assert not r.trusted
    assert r.equity_drift is None
    assert r.position_drift is None
    assert r.cash_drift is None
    assert r.reason == "truth_incomplete"


def test_missing_equity_input_untrusted():
    r = compute_drift(None, 1000.0, 5.0, 5.0, 500.0, 500.0)
    assert not r.trusted
    assert "missing" in r.reason


def test_local_equity_non_positive_untrusted():
    r = compute_drift(0.0, 0.0, 5.0, 5.0, 500.0, 500.0)
    assert not r.trusted
    assert r.reason == "local_equity <= 0"


def test_missing_position_untrusted():
    r = compute_drift(1000.0, 1000.0, None, 5.0, 500.0, 500.0)
    assert not r.trusted


def test_missing_cash_untrusted():
    r = compute_drift(1000.0, 1000.0, 5.0, 5.0, None, 500.0)
    assert not r.trusted


def test_to_dict_shape():
    r = compute_drift(1000.0, 1003.0, 5.0, 5.0, 500.0, 500.0)
    d = r.to_dict()
    assert d["trusted"] is True
    assert d["equity_drift"] == pytest.approx(3.0 / 1000.0)
    assert "position_drift" in d and "cash_drift" in d and "reason" in d
