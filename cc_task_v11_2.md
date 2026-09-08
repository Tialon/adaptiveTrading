# adaptiveTrading V11.2 — System Integration & Production Readiness

> 定位: V11.1 已交付 10 个资金正确性/自愈模块(P0-1~P0-5, P1-1~P1-5), 但多数仅作为
> 「独立测试过的模块」存在, 未真正接入主运行链路。V11.2 不再开发新模块, 而是:
> **System Integration / End-to-End Verification / Production Readiness**。
>
> 原则: 禁止新增交易策略(RSI/MACD/Bollinger/Transformer/RL/AI 自动下单/新币种/Futures/高频)。
> 冻结不变: Binance 单所 / SOLUSDT 单币 / 双仓 / 低频; AI 只分析优化、不直接下单。

## 当前状态(每单元更新)

- 版本: V11.2(进行中)
- 测试: 755/755 通过
- 分支: main

## P0 — 主链路集成与正确性

| # | 任务 | 状态 |
|---|------|------|
| P0-1 | 重新审查 V11.1 模块是否真正接入主运行链路 | ✅ |
| P0-2 | 统一 CanTrade(单一权威闸门 + 三接口) | ✅ |
| P0-3 | 审查 FundCircuitBreaker 输入 drift 定义正确性 | ✅ |
| P0-4 | CircuitBreaker 真正接入执行链 + 审计 | ✅ |
| P0-5 | End-to-End 故障注入测试 | ✅ |
| P0-6 | Financial Invariants 最终审计 | ✅ |

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

### P0-3 漂移定义正确性 ✅(2026-09-08)

**审查结论**: 原 `fund_circuit_breaker.drift_pct(diff, base)` 在 `base <= 0` 时返回 0 —— 正是
「missing-data 当 0 drift」的反模式; 且三向漂移的 numerator/denominator 未明确定义。

**交付**:

- `at50_execution/drift.py`(新建): 精确定义三向漂移语义, 消除 drift 定义歧义。
  - `equity` 分母 = `local_equity`(本地权威记账); `local <= 0` → 不可计算(记 None)。
  - `position`/`cash` 分母 = `max(|local|, |exchange|)`(对称); 单边有 → 1.0(完整失配); 双边 0 → 0。
  - `compute_drift` 纯函数: 任一输入缺失(None)/ `local_equity <= 0` / `truth_complete=False`
    → 返回 `DriftResult(trusted=False, 全维 None)`, **绝不静默当 0 drift**。
- **回归测试 14 条**(`test_v122_drift.py`): 对称/权益比例 / 零基 / 单边失配 1.0 /
  missing·truth_incomplete 不可信不 0 drift / 序列化。全量 **713/713** 通过。

---

### P0-4 CircuitBreaker 真正接入执行链 + 审计 ✅(2026-09-08)

**审查结论**: FundCircuitBreaker 此前仅作为独立模块存在, 未在对账循环里被喂入真实 drift;
`TradingGate` 的 `exchange_healthy` / `reconciled` 默认健康、`last_breaker_action` 恒 NONE,
即「默认健康假设」—— 断路器从未真正参与交易许可。

**交付**(`run.py` 接线):

- `_compute_fund_drift(symbol, last_price, truth_complete)`: 拉交易所账户(get_account)计算
  三向交易所真相(equity / position / cash), 与本地权威记账(`current_equity` / 持仓 / 由
  权益恒等式反推 local_cash)做漂移。纸面 / 无 REST / 取账户失败 → `None`(不漂移)。
- `_reconcile_loop` 实盘分支: 捕获 `exchange_truth` 原始 findings → 推导 `truth_complete`
  (无 `truth_incomplete`/`pagination_exhausted`)→ `compute_drift` → `FundCircuitBreaker.assess`
  → `BreakerDecision`。漂移不可信时置 `exchange_healthy=False` 并跳过判定(不误判)。
- `_apply_breaker_decision`: 单一处置点 —— REDUCE_ONLY → `reduce_only`, PAUSE → `pause`,
  KILL → `kill_switch.arm` + 持久冻结; 动作反馈到 `trading_gate.last_breaker_action`。
- `_record_breaker_decision`: 决策审计落库 `RiskEvent`(event_type=`fund_breaker`, detail 为
  JSON, 含 source/timestamp/三向漂移/action/reason/lifecycle_state/risk_state)。
- `_apply_verdict`: 对账判定反馈到闸门 —— `reconciled` = 无 actionable 差异,
  `exchange_healthy` = 无 `api_error`/`truth_incomplete`/`pagination_exhausted`, **消除默认健康假设**。
- **回归测试 10 条**(`test_v123_fund_breaker_chain.py`): 真相→漂移→assess→决策端到端纯链路
  (一致账本 NONE / 权益逐级收紧 REDUCE_ONLY·PAUSE·KILL / 持仓·现金 REDUCE_ONLY·PAUSE /
  truth_incomplete·missing·零权益绝不误判 KILL)。全量 **723/723** 通过。

---

### P0-5 端到端故障注入 ✅(2026-09-08)

**交付**: `tests/unit/test_v124_fault_injection.py` —— 系统级故障测试(非纯函数), 用真实
`RiskManager + SystemLifecycle + FundCircuitBreaker + TradingGate` 装配成与 run.py 一致的
闸门栈, 对 V11.2 路线图 **24 类故障**逐一注入并断言最终态 + 核心不变量。

**核心不变量**: 故障发生**绝不「异常 → 继续 BUY」** —— 凡降级/急停故障, `can_open_position`
必为 False; 同时四维最终态(闸门 / 生命周期 / 风险态 / 急停)明确且一致。

24 场景最终态分布:

| 最终态 | 场景 |
|--------|------|
| PASS | 成交后崩溃(重建)/ 重复 WS·REST 成交(幂等)/ 部分成交 / 部分成交后撤单 / 未知订单(收敛)/ fee=USDT·SOL·BNB / 熔断恢复 |
| DEGRADED | REST timeout(交易所不健康)/ WS 断开(连接未就绪)/ myTrades 分页不完整(暂停) |
| REDUCE_ONLY | 持仓漂移 / 现金漂移 / 熔断触发 |
| RECOVERY | 恢复失败(recover_unresolved → 暂停, 不持久急停) |
| KILLED | DB insert 失败 / DB 回滚 / 下单后崩溃 / 缺 myTrades(fill_truth_missing)/ 权益漂移 / 对账冲突 / 启动对账失败 |

**定向补充 2 条**: KILLED 不能裸 reset 到可开仓(需 `RECOVERY_CHECK → confirm_recovered` +
人工解除急停); `truth_incomplete` 漂移不可信不触发熔断但交易所不健康 → 禁开仓。

**回归测试 26 条**, 全量 **749/749** 通过。执行/账务类故障的账务正确性沿用 `test_v107_chaos.py`
与 fee 计价测试, 本片聚焦「故障 → 闸门最终态」的系统级不变量。

---

### P0-6 财务不变量最终审计 ✅(2026-09-08)

**交付**: `tests/unit/test_v125_financial_invariants.py` —— 统一 financial invariant test,
对贯穿「订单→成交→账本→持仓→lot」链路的 5 条财务守恒不变量做最终收口, **全部计算允许手续费**
(fee 显式参与恒等式, 不假设零手续费), 外加「守恒破坏 → 禁开仓」系统级不变量。

- **Base Asset**: `Σ BUY filled - Σ SELL filled == position.quantity`(订单 ↔ 持仓, 无负仓, DB 镜像一致)。
- **Lots**: `Σ open lot.quantity == position.quantity`(lot ↔ 持仓, `lot_tracker.reconcile` 无差异)。
- **Sell Allocation**: `Σ SellAllocation.quantity == 该笔 SELL filled`(卖出分配 ↔ 订单, 不跨 lot 超卖)。
- **Cash**: `Σ AccountLedger.USDT 变更 == Σ SELL 成交额 - Σ BUY 成交额 - Σ fee_quote`(账本 ↔ 成交, 含手续费)。
- **Equity**: 平仓后 `cash == initial_cash + realized_pnl`, 且 `rm.equity == initial + realized`(现金 ↔ 已实现盈亏)。
- **守恒破坏 → 禁开仓**: 篡改 lot 破坏守恒 → `lot_tracker.reconcile` 检出 → 对账矩阵单一内部对账器收敛为
  `RECOVERY_REQUIRED` → 暂停 → `TradingGate.can_open_position()` 为 False。

**回归测试 6 条**, 全量 **755/755** 通过。P0 主链路集成与正确性六单元全部完成。

(后续单元追加于此)
