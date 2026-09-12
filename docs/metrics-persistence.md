# Metrics 持久化方案评估(V11.3 P1-1)

> 结论先行: **保持内存 `MetricsStore`, 不新增持久化表。** 指标是「运行期健康」,
> 重启归零语义正确; 需要跨重启趋势时再按 `position_snapshot` 同模式补快照表。

## 1. 现状

`MetricsStore`(`at60_execution/observability.py`)为纯内存, 采集四类信号:

| 类别 | 字段 | 用途 |
|------|------|------|
| 计数器 | `orders_total` / `orders_failed` / `recoveries` / `recovery_streak` / `reconcile_*` / `breaker_*` | 累计量 / 失败率 / 连续恢复 |
| 仪表 | `data_gap_seconds` / `reconcile_drift_pct` | 当前瞬时值 |
| 样本 | `execution_latency_ms`(P0-10 已上限 10k) | 延迟 P95 |
| 归因 | `strategy_pnl` | 策略 PnL 汇总 |

消费点: (1) `evaluate_alerts` 每 5s 阈值告警 → 日志 + `/api/metrics`; (2) `strategy_attribution`
策略归因; (3) `snapshot()` 供面板/日志。**进程退出即丢失。**

## 2. 关键判断: 指标是否需要持久化?

指标分两类, 语义不同:

- **运行期健康**(counters/gauges): 失败率、恢复连续数、数据缺口 —— 描述「当前进程是否健康」。
  重启后从零重新累计是**正确语义**(旧进程的失败率不应污染新进程)。
- **事后复盘**(PnL / 趋势): 已由 DB 源表覆盖 —— `account_ledger`(逐笔)、`position_snapshot`
  (60s 权益曲线)、`trades` / `orders` / `risk_events` / `closed_trades` / `strategy_performance`。
  `strategy_pnl` 是这些的运行时投影, 从 DB 可随时重建。

**结论**: 指标本身的持久化价值低; 真正需要跨重启的「复盘数据」已经在 DB 里。

## 3. 方案对比

| 方案 | 说明 | 优劣 | 判定 |
|------|------|------|------|
| A. 保持内存(现状) | 零改动零开销 | 重启归零(语义正确); 无跨重启趋势 | ✅ **采纳** |
| B. 周期快照落库 | 仿 `position_snapshot` 新增 `metrics_snapshot` 表 + 采样循环 | 可得趋势; 但需定义保留策略 + 写开销 + 无当下消费者 | ⏸ 有消费者再上 |
| C. 日志 JSON | 现有 structlog 已输出 `snapshot` | 可 grep; 结构化查询弱 | 已具备, 作兜底 |
| D. Prometheus / 时序库 | 标准可观测栈 | 违反「零新依赖」冻结约束; 单机无人值守过重 | ❌ 不做 |

## 4. 推荐与触发条件

- **本次(V11.3)**: 方案 A。P0-10 已完成样本有界 + 恢复计数接线 + 告警降噪, 指标闭环完整;
  跨重启复盘由 DB 源表覆盖。
- **触发升级(未来, 出现以下任一消费者时)**:
  1. 每日复盘报告需要「本周订单失败率 / 恢复次数趋势」;
  2. AI 优化需要「策略归因历史序列」。
  届时按 `position_snapshot` 同模式: 新增 `metrics_snapshot`(含 `timestamp` + `payload_json`
  + 关键计数/仪表列), 在现有 `snapshot-loop`(60s)并行采样, 保留 N 天, 并同步
  `SCHEMA_VERSION` + `test_v129_schema_audit.py`。
- **明确不做**: 时序数据库 / Prometheus / 外部监控依赖(违反零新依赖 + 单机无人值守约束)。

## 5. 相关文件

- `at60_execution/observability.py` —— `MetricsStore` / `evaluate_alerts` / `record_*`
- `run.py::_risk_loop` —— 告警评估 + 降噪(仅集合变化时告警)
- `at90_web/web_api_routes.py::/api/metrics` —— 快照/告警/归因导出
- `at60_execution/execution_executor.py::_snapshot_loop` —— `position_snapshot` 采样(未来挂载点)
