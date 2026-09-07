"""
Gaussian HMM(V9.0 M3.3) — 纯 Python 实现, 零新依赖(无 numpy/scipy)

用途: 概率式市场状态识别积木。对角协方差高斯 HMM, log 域前向-后向(Baum-Welch)
防下溢, Viterbi 解码。**不接入实盘 regime 判定**(默认 regime_hmm_enabled=false)。

接口:
    model = GaussianHMM(n_states=3, n_features=4)
    model.fit(observations, n_iter=20)
    states = model.predict(observations)          # Viterbi 状态序列
    proba = model.filter_proba(observations)      # 末步前向概率(状态概率分布)
    model.save(path); model2 = GaussianHMM.load(path)
"""

import json
import math
import random
from typing import Any, Optional

_LOG_2PI = math.log(2 * math.pi)
_VAR_FLOOR = 1e-6


def _logsumexp(values: list[float]) -> float:
    """数值稳定的 log(sum(exp(values)))"""
    if not values:
        return -float("inf")
    m = max(values)
    if m == -float("inf"):
        return -float("inf")
    return m + math.log(sum(math.exp(v - m) for v in values))


def _normalize_log(logprobs: list[float]) -> list[float]:
    """log 域概率 -> 归一化概率"""
    z = _logsumexp(logprobs)
    if z == -float("inf"):
        n = len(logprobs)
        return [1.0 / n] * n
    return [math.exp(v - z) for v in logprobs]


class GaussianHMM:
    """对角协方差高斯 HMM(log 域前向-后向)"""

    def __init__(self, n_states: int = 3, n_features: int = 4, seed: Optional[int] = None):
        if n_states < 1 or n_features < 1:
            raise ValueError("n_states / n_features 必须 >= 1")
        self.n_states = n_states
        self.n_features = n_features
        # 初始: 均匀起始概率 + 略粘滞转移矩阵(自转 0.9, 其余均分)
        self.startprob = [1.0 / n_states] * n_states
        stick = 0.9
        other = (1.0 - stick) / max(1, n_states - 1)
        self.transmat = [
            [stick if i == j else other for j in range(n_states)]
            for i in range(n_states)
        ]
        # 均值 0, 对角方差 1
        self.means = [[0.0] * n_features for _ in range(n_states)]
        self.diag_var = [[1.0] * n_features for _ in range(n_states)]
        self._seed = seed
        self._log_startprob = [math.log(p) for p in self.startprob]
        self._log_transmat = [
            [math.log(p) for p in row] for row in self.transmat
        ]

    # ---------- 发射概率(log 域) ----------

    def _log_gauss(self, x: float, mean: float, var: float) -> float:
        v = max(var, _VAR_FLOOR)
        d = x - mean
        return -0.5 * (_LOG_2PI + math.log(v) + d * d / v)

    def _log_emission(self, observations: list[list[float]]) -> list[list[float]]:
        """log_emit[t][s] = log P(o_t | state s)"""
        T = len(observations)
        out = [[0.0] * self.n_states for _ in range(T)]
        for t, obs in enumerate(observations):
            for s in range(self.n_states):
                out[t][s] = sum(
                    self._log_gauss(obs[f], self.means[s][f], self.diag_var[s][f])
                    for f in range(self.n_features)
                )
        return out

    # ---------- 前向-后向 ----------

    def _forward_backward(self, observations: list[list[float]]):
        """返回 (alpha, beta, log_lik), 均为 log 域"""
        T = len(observations)
        n = self.n_states
        emit = self._log_emission(observations)

        alpha = [[0.0] * n for _ in range(T)]
        for s in range(n):
            alpha[0][s] = self._log_startprob[s] + emit[0][s]
        for t in range(1, T):
            for s in range(n):
                alpha[t][s] = emit[t][s] + _logsumexp(
                    [alpha[t - 1][sp] + self._log_transmat[sp][s] for sp in range(n)]
                )

        beta = [[0.0] * n for _ in range(T)]
        for t in range(T - 2, -1, -1):
            for s in range(n):
                beta[t][s] = _logsumexp(
                    [
                        self._log_transmat[s][sp] + emit[t + 1][sp] + beta[t + 1][sp]
                        for sp in range(n)
                    ]
                )

        log_lik = _logsumexp(alpha[T - 1])
        return alpha, beta, log_lik

    # ---------- 训练(Baum-Welch) ----------

    def fit(self, observations: list[list[float]], n_iter: int = 20) -> "GaussianHMM":
        """Baum-Welch 重估(对角协方差)"""
        T = len(observations)
        if T < 2:
            return self
        n = self.n_states
        f = self.n_features

        # 打破对称性: 用数据范围线性铺开初始均值 + 随机扰动(否则两状态均值相同, EM 坍缩)
        rng = random.Random(self._seed if self._seed is not None else 0)
        mins = [min(o[j] for o in observations) for j in range(f)]
        maxs = [max(o[j] for o in observations) for j in range(f)]
        for s in range(n):
            frac = s / max(1, n - 1)
            for j in range(f):
                spread = maxs[j] - mins[j]
                noise = rng.gauss(0.0, 0.1 * spread + 1e-6)
                self.means[s][j] = mins[j] + spread * frac + noise

        for _ in range(n_iter):
            alpha, beta, log_lik = self._forward_backward(observations)

            # gamma[t][s] = P(state_t = s | O)
            gamma = [[0.0] * n for _ in range(T)]
            for t in range(T):
                for s in range(n):
                    gamma[t][s] = alpha[t][s] + beta[t][s] - log_lik

            # xi[t][s][sp] = P(state_t=s, state_{t+1}=sp | O)
            emit = self._log_emission(observations)
            xi = [[[0.0] * n for _ in range(n)] for _ in range(T - 1)]
            for t in range(T - 1):
                for s in range(n):
                    for sp in range(n):
                        xi[t][s][sp] = (
                            alpha[t][s]
                            + self._log_transmat[s][sp]
                            + emit[t + 1][sp]
                            + beta[t + 1][sp]
                            - log_lik
                        )

            # 概率域累计(归一化以稳)
            gamma_p = [[max(math.exp(v), 0.0) for v in row] for row in gamma]
            for t in range(T):
                srow = sum(gamma_p[t])
                if srow > 0:
                    gamma_p[t] = [v / srow for v in gamma_p[t]]

            xi_p = [
                [[max(math.exp(v), 0.0) for v in row] for row in xi[t]]
                for t in range(T - 1)
            ]

            # 起始概率
            self.startprob = gamma_p[0]
            self._log_startprob = [math.log(max(p, 1e-12)) for p in self.startprob]

            # 转移矩阵
            for s in range(n):
                denom = sum(gamma_p[t][s] for t in range(T - 1))
                for sp in range(n):
                    numer = sum(xi_p[t][s][sp] for t in range(T - 1))
                    self.transmat[s][sp] = numer / denom if denom > 0 else 1.0 / n
                # 归一化该行
                row_sum = sum(self.transmat[s])
                if row_sum > 0:
                    self.transmat[s] = [v / row_sum for v in self.transmat[s]]
            self._log_transmat = [
                [math.log(max(p, 1e-12)) for p in row] for row in self.transmat
            ]

            # 均值 / 方差
            for s in range(n):
                weight = sum(gamma_p[t][s] for t in range(T))
                if weight <= 0:
                    continue
                for j in range(f):
                    mean = sum(gamma_p[t][s] * observations[t][j] for t in range(T)) / weight
                    var = sum(
                        gamma_p[t][s] * (observations[t][j] - mean) ** 2
                        for t in range(T)
                    ) / weight
                    self.means[s][j] = mean
                    self.diag_var[s][j] = max(var, _VAR_FLOOR)

        return self

    # ---------- 解码 ----------

    def predict(self, observations: list[list[float]]) -> list[int]:
        """Viterbi 解码最可能状态序列"""
        T = len(observations)
        n = self.n_states
        if T == 0:
            return []
        emit = self._log_emission(observations)

        delta = [[0.0] * n for _ in range(T)]
        psi = [[0] * n for _ in range(T)]
        for s in range(n):
            delta[0][s] = self._log_startprob[s] + emit[0][s]
        for t in range(1, T):
            for s in range(n):
                best = max(
                    range(n),
                    key=lambda sp: delta[t - 1][sp] + self._log_transmat[sp][s],
                )
                psi[t][s] = best
                delta[t][s] = emit[t][s] + delta[t - 1][best] + self._log_transmat[best][s]

        path = [0] * T
        path[T - 1] = max(range(n), key=lambda s: delta[T - 1][s])
        for t in range(T - 2, -1, -1):
            path[t] = psi[t + 1][path[t + 1]]
        return path

    def filter_proba(self, observations: list[list[float]]) -> list[float]:
        """末步前向概率 -> 状态概率分布(归一化)"""
        if not observations:
            return [1.0 / self.n_states] * self.n_states
        alpha, _, _ = self._forward_backward(observations)
        return _normalize_log(alpha[-1])

    # ---------- 序列化 ----------

    def save(self, path: str) -> None:
        data: dict[str, Any] = {
            "n_states": self.n_states,
            "n_features": self.n_features,
            "startprob": self.startprob,
            "transmat": self.transmat,
            "means": self.means,
            "diag_var": self.diag_var,
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, path: str) -> "GaussianHMM":
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        m = cls(n_states=data["n_states"], n_features=data["n_features"])
        m.startprob = list(data["startprob"])
        m.transmat = [list(row) for row in data["transmat"]]
        m.means = [list(row) for row in data["means"]]
        m.diag_var = [list(row) for row in data["diag_var"]]
        m._log_startprob = [math.log(max(p, 1e-12)) for p in m.startprob]
        m._log_transmat = [
            [math.log(max(p, 1e-12)) for p in row] for row in m.transmat
        ]
        return m
