"""V9.0 M3.3: HMM Regime 测试(纯 Python GaussianHMM)

验证: 合成两状态数据 fit 后 Viterbi 状态恢复、filter_proba 归一、save/load 往返一致。
"""

import random

import pytest

from at20_analytics.regime_hmm import GaussianHMM


def _two_state_data(seed: int = 42, n_each: int = 200) -> list[list[float]]:
    """两个明显分离的高斯块(2 维), 用于验证状态分割"""
    rng = random.Random(seed)
    data = []
    for _ in range(n_each):  # 状态 A: 均值 0
        data.append([rng.gauss(0.0, 0.2), rng.gauss(0.0, 0.2)])
    for _ in range(n_each):  # 状态 B: 均值 5
        data.append([rng.gauss(5.0, 0.2), rng.gauss(5.0, 0.2)])
    return data


class TestGaussianHMM:
    def test_fit_viterbi_recovers_states(self):
        data = _two_state_data()
        model = GaussianHMM(n_states=2, n_features=2, seed=42)
        model.fit(data, n_iter=20)

        path = model.predict(data)
        assert len(path) == len(data)
        assert len(set(path)) == 2  # 两个状态都被用到

        # 前半段与后半段的主导状态不同(正确分割两个块)
        n = len(data)
        first_half = max(set(path[: n // 2]), key=path[: n // 2].count)
        second_half = max(set(path[n // 2:]), key=path[n // 2:].count)
        assert first_half != second_half

    def test_filter_proba_normalized(self):
        data = _two_state_data(n_each=100)
        model = GaussianHMM(n_states=3, n_features=2, seed=1)
        model.fit(data, n_iter=10)

        proba = model.filter_proba(data)
        assert len(proba) == 3
        assert all(0.0 <= p <= 1.0 for p in proba)
        assert sum(proba) == pytest.approx(1.0, abs=1e-6)

    def test_save_load_roundtrip(self, tmp_path):
        data = _two_state_data(n_each=80)
        model = GaussianHMM(n_states=2, n_features=2, seed=7)
        model.fit(data, n_iter=15)

        p = tmp_path / "hmm.json"
        model.save(str(p))
        loaded = GaussianHMM.load(str(p))

        assert loaded.n_states == model.n_states
        assert loaded.n_features == model.n_features
        assert loaded.startprob == pytest.approx(model.startprob)
        # 嵌套矩阵需展平后近似比较
        flat_t = [x for row in loaded.transmat for x in row]
        flat_t0 = [x for row in model.transmat for x in row]
        flat_m = [x for row in loaded.means for x in row]
        flat_m0 = [x for row in model.means for x in row]
        flat_v = [x for row in loaded.diag_var for x in row]
        flat_v0 = [x for row in model.diag_var for x in row]
        assert flat_t == pytest.approx(flat_t0)
        assert flat_m == pytest.approx(flat_m0)
        assert flat_v == pytest.approx(flat_v0)
        # 加载后前向概率与原始一致
        assert loaded.filter_proba(data) == pytest.approx(model.filter_proba(data))

    def test_empty_observations(self):
        model = GaussianHMM(n_states=2, n_features=2)
        assert model.predict([]) == []
        proba = model.filter_proba([])
        assert sum(proba) == pytest.approx(1.0)


class TestRegimeHook:
    def test_hmm_hook_default_off(self):
        """regime_hmm_enabled=false 时, 实盘 regime 分类零变化"""
        from at20_analytics.regime import MarketRegimeEngine

        engine = MarketRegimeEngine()
        # 未挂载 HMM -> filter 返回 None, 不产生 hmm_proba
        assert engine.hmm_filter([[0.0, 0.0, 1.0, 0.0]]) is None

        a = engine.evaluate(
            symbol="SOLUSDT", symbol_trend="up", symbol_ema_fast=10.0,
            symbol_ema_slow=9.0, recent_high=10.5, recent_low=9.5,
            volume_ratio=1.0, delta_ratio=0.1, cvd_rising=True,
            btc_trend="up", btc_change_24h=3.0,
        )
        assert a.regime in ("BULL", "NORMAL", "SIDEWAY", "VOLATILE", "BEAR", "PANIC")
        assert a.hmm_proba is None  # 默认不挂载, 无 HMM 概率
