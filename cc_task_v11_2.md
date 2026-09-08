# adaptiveTrading V11.2 — System Integration & Production Readiness

> 定位: V11.1 已交付 10 个资金正确性/自愈模块(P0-1~P0-5, P1-1~P1-5), 但多数仅作为
> 「独立测试过的模块」存在, 未真正接入主运行链路。V11.2 不再开发新模块, 而是:
> **System Integration / End-to-End Verification / Production Readiness**。
>
> 原则: 禁止新增交易策略(RSI/MACD/Bollinger/Transformer/RL/AI 自动下单/新币种/Futures/高频)。
> 冻结不变: Binance 单所 / SOLUSDT 单币 / 双仓 / 低频; AI 只分析优化、不直接下单。

## 当前状态(每单元更新)

- 版本: V11.2(进行中)
- 测试: 699/699 通过
- 分支: main

## P0 — 主链路集成与正确性

| # | 任务 | 状态 |
|---|------|------|
| P0-1 | 重新审查 V11.1 模块是否真正接入主运行链路 | ✅ |
| P0-2 | 统一 CanTrade(单一权威闸门 + 三接口) | ✅ |
| P0-3 | 审查 FundCircuitBreaker 输入 drift 定义正确性 | ⏳ |
| P0-4 | CircuitBreaker 真正接入执行链 + 审计 | ⏳ |
| P0-5 | End-to-End 故障注入测试 | ⏳ |
| P0-6 | Financial Invariants 最终审计 | ⏳ |

## P1 — 生产就绪

| # | 任务 | 状态 |
|---|------|------|
| P1-1 | 真实运行生命周期 | ⏳ |
| P1-2 | Observability 真正接入 | ⏳ |
| P1-3 | Backtest 最终验收 | ⏳ |
| P1-4 | Production Configuration Audit | ⏳ |
| P1-5 | 数据库迁移审计 | ⏳ |
| P1-6 | 代码死路径审计 | ⏳ |
| P1-7 | 文档同步(强制) | ⏳ |

---

### P0-1 / P0-2 统一 CanTrade ✅(2026-09-08)

**审查结论**: 启动前 V11.1 的 SystemLifecycle / FundCircuitBreaker / MetricsStore 三个模块
仅存在文件与单元测试, 未在 run.py 实例化或接线; 交易许可分散在三处(`_on_signal` 用
`risk_manager.can_buy/can_sell`、`_apply_core_action` 同样、`RiskManager.check` 内部再判一次),
无单一权威闸门。

**交付**:

- `at60_risk/trading_gate.py`(新建): `TradingGate` 单一权威交易闸门, 组合六维:
  `SystemLifecycle.can_trade` AND `RiskState.can_trade` AND `MarketDataHealthy` AND
  `ExchangeHealthy` AND `ReconciliationHealthy` AND `CircuitBreaker.can_trade`。
- 三接口: `can_open_position()`(开新仓)/ `can_reduce_position()`(减仓)/ `can_cancel_order()`(撤单)。
- 方向语义: 正常(READY/TRADING + NORMAL)BUY/SELL 皆可; DEGRADED/RECOVERY 禁 BUY 可减;
  SAFE_MODE 禁 BUY、数据可信时可减; KILLED 全禁(禁 BUY、禁减仓、禁自动恢复)。
- `run.py` 装配: `initialize()` 实例化 `SystemLifecycle`(warm_up 起步, 末尾推进到 READY/TRADING,
  急停时停在 READY)+ `FundCircuitBreaker` + `MetricsStore` + `TradingGate`;
  `_on_signal` 与 `_apply_core_action` 统一改走 `trading_gate.can_open_position/can_reduce_position`;
  `_risk_loop` 每轮更新闸门健康信号(连接/行情健康)。
- `at10_web/web_state.py`: 注册 `trading_gate`/`metrics`, `summary()` 输出闸门快照 + 指标快照。
- **回归测试 20 条**(`test_v121_trading_gate.py`): 六维组合 / 三接口 / 降级恢复期禁买可减 /
  SAFE_MODE 数据可信可减 / KILLED·急停·熔断全禁 / 撤单除 STOPPED 放行 / 快照形状。全量 **699/699** 通过。

> 注: 闸门健康信号 `exchange_healthy` / `reconciled` / 资金熔断动作当前默认健康, 待 P0-3/P0-4
> 在对账循环中由真实 drift 与对账判定更新(消除默认健康假设)。

---

(后续单元追加于此)
