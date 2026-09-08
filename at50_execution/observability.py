"""生产可观测性(V11.1 P1-4)

统一采集执行延迟 / 对账漂移 / 恢复次数 / 订单失败率 / 数据缺口 / 策略归因六类指标,
并提供「阈值告警」判定, 让「异常下不错误改账」可被观测、可被告警。

设计:
- `MetricsStore`: 内存计数器/仪表/样本/策略归因(纯数据, 无副作用)。
- 纯函数 `order_failure_rate` / `percentile_rank`: 派生指标。
- 纯函数 `evaluate_alerts(store, thresholds)`: 阈值告警(可独立测试)。
- `strategy_attribution(store)`: 策略 PnL 归因汇总。

阈值默认值(可由 AlertThresholds 覆盖):
- 订单失败率 > 5% → CRITICAL。
- 对账漂移 > 2% → WARNING。
- 数据缺口 > 300s → WARNING。
- 执行延迟 P95 > 5000ms → WARNING。
- 连续恢复次数 >= 5 → WARNING(提示底层反复异常, 需人工关注)。
"""

import math
from dataclasses import dataclass, field
from typing import Any

from at01_common.logger import LoggerMixin


@dataclass
class Alert:
    """一条告警。"""

    name: str
    severity: str  # WARNING / CRITICAL
    message: str
    value: float
    threshold: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "severity": self.severity,
            "message": self.message,
            "value": round(self.value, 4),
            "threshold": self.threshold,
        }


@dataclass
class AlertThresholds:
    """告警阈值(可覆盖)。"""

    order_failure_rate: float = 0.05
    reconcile_drift_pct: float = 0.02
    data_gap_seconds: float = 300.0
    execution_latency_p95_ms: float = 5000.0
    recovery_streak: int = 5


def order_failure_rate(total: int, failures: int) -> float:
    """订单失败率 = 失败数 / 总订单数(0 订单 → 0)。"""
    return failures / total if total > 0 else 0.0


def percentile_rank(samples: list[float], p: float) -> float:
    """nearest-rank 百分位(p ∈ [0, 100]); 空样本返回 0。"""
    if not samples:
        return 0.0
    s = sorted(samples)
    k = max(1, int(math.ceil(p / 100.0 * len(s))))
    return s[k - 1]


class MetricsStore:
    """指标采集存储(内存, 单进程; 供采集方 ingest, 供告警/归因读取)。"""

    def __init__(self):
        self.counters: dict[str, int] = {}
        self.gauges: dict[str, float] = {}
        self.samples: dict[str, list[float]] = {}
        self.strategy_pnl: dict[str, float] = {}

    # ---- 采集 ----

    def incr(self, name: str, n: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + n

    def gauge(self, name: str, value: float) -> None:
        self.gauges[name] = value

    def observe(self, name: str, value: float) -> None:
        self.samples.setdefault(name, []).append(value)

    def add_strategy_pnl(self, strategy: str, pnl: float) -> None:
        self.strategy_pnl[strategy] = self.strategy_pnl.get(strategy, 0.0) + pnl

    # ---- 读取 ----

    def get_counter(self, name: str) -> int:
        return self.counters.get(name, 0)

    def get_gauge(self, name: str) -> float:
        return self.gauges.get(name, 0.0)

    def percentile(self, name: str, p: float) -> float:
        return percentile_rank(self.samples.get(name, []), p)

    def order_failure_rate(self) -> float:
        return order_failure_rate(
            self.get_counter("orders_total"), self.get_counter("orders_failed")
        )

    def snapshot(self) -> dict[str, Any]:
        """指标快照(供日志/面板)。"""
        return {
            "orders_total": self.get_counter("orders_total"),
            "orders_failed": self.get_counter("orders_failed"),
            "order_failure_rate": round(self.order_failure_rate(), 4),
            "recoveries": self.get_counter("recoveries"),
            "recovery_streak": self.get_counter("recovery_streak"),
            "data_gaps": self.get_counter("data_gaps"),
            "data_gap_seconds": self.get_gauge("data_gap_seconds"),
            "reconcile_drift_pct": self.get_gauge("reconcile_drift_pct"),
            "execution_latency_p95_ms": round(self.percentile("execution_latency_ms", 95.0), 2),
            "strategy_pnl": {k: round(v, 2) for k, v in self.strategy_pnl.items()},
        }


def evaluate_alerts(store: MetricsStore, thresholds: AlertThresholds | None = None) -> list[Alert]:
    """阈值告警判定(纯函数): 返回命中的告警列表(空 = 无告警)。"""
    t = thresholds or AlertThresholds()
    alerts: list[Alert] = []

    fr = store.order_failure_rate()
    if fr > t.order_failure_rate:
        alerts.append(Alert(
            "order_failure_rate", "CRITICAL",
            f"订单失败率 {fr:.1%} 超限", fr, t.order_failure_rate,
        ))

    drift = store.get_gauge("reconcile_drift_pct")
    if drift > t.reconcile_drift_pct:
        alerts.append(Alert(
            "reconcile_drift", "WARNING",
            f"对账漂移 {drift:.2%} 超限", drift, t.reconcile_drift_pct,
        ))

    gap = store.get_gauge("data_gap_seconds")
    if gap > t.data_gap_seconds:
        alerts.append(Alert(
            "data_gap", "WARNING",
            f"数据缺口 {gap:.0f}s 超限", gap, t.data_gap_seconds,
        ))

    p95 = store.percentile("execution_latency_ms", 95.0)
    if p95 > t.execution_latency_p95_ms:
        alerts.append(Alert(
            "execution_latency_p95", "WARNING",
            f"执行延迟 P95 {p95:.0f}ms 超限", p95, t.execution_latency_p95_ms,
        ))

    streak = store.get_counter("recovery_streak")
    if streak >= t.recovery_streak:
        alerts.append(Alert(
            "recovery_streak", "WARNING",
            f"连续恢复 {streak} 次, 底层反复异常", float(streak), float(t.recovery_streak),
        ))

    return alerts


def strategy_attribution(store: MetricsStore) -> list[dict[str, float]]:
    """策略 PnL 归因(按 PnL 降序)。"""
    total = sum(store.strategy_pnl.values())
    rows = []
    for strategy, pnl in sorted(store.strategy_pnl.items(), key=lambda kv: kv[1], reverse=True):
        rows.append({
            "strategy": strategy,
            "pnl": pnl,
            "share": (pnl / total) if total else 0.0,
        })
    return rows
