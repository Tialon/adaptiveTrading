# 项目进度日志

> 记录每个开发阶段的关键交付与验证结论

## V11.1 — Financial Correctness & Self-Healing(进行中, 2026-09-08)

**定位: 承接 V11.0「证明异常下不错误改账」, 补上 Exchange Truth 完整性、手续费计价、账本重建、
SELL 自愈、对账分级五大资金正确性闭环。**

### P0-1 Exchange Truth V2(已完成)

- `get_my_trades_all` 返回 `MyTradesResult`(去重 + 重复/跳号/翻页耗尽检测), 不再静默假定「已拉全」。
- 交易所真相对账窗口从本地订单 `created_at` 推导(减 60s buffer), 去掉硬编码「15min / 200 笔」。
- 分页耗尽 → `truth_incomplete` / `pagination_exhausted`, 只降级(pause 自动恢复)不冻结;
  `trade_duplicate` / `trade_id_gap` 仅可观测性告警(去重已消除资金影响, 跳号在 myTrades 中属正常)。
- 不完整时跳过逐订单成交核对与孤儿检测, 避免误判 `fill_truth_missing` / `fill_truth_mismatch` / `orphan_trade`。
- **新增测试 9 条**, 全量 **521/521** 通过。

### P0-2 Fee Accounting Contract(已完成)

- 新建 `at50_execution/fee_calculator.py`: 统一 `FeeCalculator` —— USDT(quote)/SOL(base)可折算计价;
  其它资产(如 BNB)返回 `unpriced` 降级,**不再静默记 fee=0**; `FillFee`/`FeeResult` 承载
  `ZERO`/`PRICED`/`UNPRICED` 三态。
- `OrderFill` 新增 `fee_quote` + `fee_valuation_status` 两列, `_record_fills` 逐笔落真实手续费与计价状态。
- `_compute_fill_metrics` 返回 `(avg, fee_quote, fee_unpriced)`; `_ingest_fills` 检测到不可计价手续费 →
  告警 + `risk.pause` 降级(自动恢复, 不冻结)。
- **新增测试 10 条**, 全量 **531/531** 通过。存量库迁移见 runbook(order_fills 补两列)。

### P0-3 Ledger Reconstruction Engine(已完成)

- 新建 `at50_execution/ledger_reconstruction.py`: 从交易所真相(myTrades 全量成交)重建账务状态,
  链 `Exchange Truth → Trades → Orders → Buy Lots → Sell Allocations → Position → Cash → Ledger → Equity`。
- 幂等: 纯函数 `build_plan` 同输入同输出; `apply` 单事务「先清后插」可重复执行。
- dry-run(默认, 只产出计划)/ apply(无歧义才落库)。
- 守恒检查(base 守恒 / 无超卖 / 卖出分配覆盖 / 现金守恒)任一失败 → SAFE_MODE;
  歧义(成交历史不完整 / 缺现金锚 / 超卖 / 成交方向不一致 / 无交易所客户端)→ SAFE_MODE 拒绝 apply。
- 现金锚 `cash_before` 缺失即 SAFE_MODE(不猜绝对现金); 不可计价手续费不触发 SAFE_MODE 但权益置 None。
- **新增测试 12 条**, 全量 **543/543** 通过。

### P0-4 SELL Recovery(已完成)

- 消除 RECOVERY_REQUIRED SELL → 永久人工冻结: SELL 记账失败时 in-memory lot 已被
  `allocate_sell` 消费(内存先改、DB 事务整体回滚), 复用 `apply_recovered_fill` 会对已分歧
  内存二次消费; 故从 DB 开仓 `PositionLot`(权威未消费态)确定性重放 FIFO 分配。
- 新增 `ExecutionEngine.rebuild_sell_accounting`: 单事务落 `SellAllocation` + 减 lot +
  更新 `Position`(平均成本口径: 卖出不改 avg_price、清仓归零)+ `Order`(FILLED + RECOVERED),
  完成后 `_resync_sell_memory` 重同步内存持仓/FIFO lot 队列(覆盖分歧内存)。
- `order_recovery.py::_recover_accounting` 对 SELL 走 `_recover_sell_accounting`(本地成交数据
  缺失时从交易所真相补齐), 移除「SELL 保守冻结交人工」。
- **新增测试 7 条**(`test_v114_sell_recovery.py`)+ 更新 `test_v107_order_recovery.py` SELL 用例,
  全量 **550/550** 通过。无新表无迁移。

### P0-5 Reconciliation Matrix(已完成)

- 统一各对账器差异处置为单一判定点: 新建 `at50_execution/reconciliation_matrix.py`
  (`Severity` 四态 + `Finding`/`Verdict`/`ReconciliationMatrix`), 消除「各对账器分散、
  各自独立 arm kill」现状。
- 核心规则「单一对账器不得 kill」: 跨源资金级差异(equity_drift/orphan_trade/exchange_only/
  fill_truth_missing/fill_truth_mismatch)单源即 KILLED; 本地 DB 内部一致性破坏(fill_*/ledger_*/
  buy_lot/sell_alloc/lot_sum)单一对账器只 RECOVERY_REQUIRED(自愈不 kill), 需 ≥2 独立对账器
  同周期佐证才升级 KILLED; recover_unresolved → RECOVERY_REQUIRED; 数据不完整/现金异常 →
  DEGRADED; api_error/trade_duplicate/trade_id_gap 等 → PASS 仅记录。
- `run.py::_reconcile_loop` 重构为「摄入 findings → `matrix.verdict()` → `_apply_verdict`」,
  新增 `_apply_verdict`(PASS 无动作 / DEGRADED·RECOVERY_REQUIRED pause / KILLED arm+persist)。
- **新增测试 38 条**(`test_v115_reconciliation_matrix.py`), 全量 **588/588** 通过。无新表无迁移。

### P1-1 Backtest V2(已完成)

- 新建 `at70_backtest/backtest_robustness.py`: 用「鲁棒性评分」取代「单一收益」作为策略上线判据。
- 四维矩阵(5×6×5×5 = 750 格): 时间窗口(7/30/90/180/365 天)× 市场态(BULL/NORMAL/SIDEWAY/VOLATILE/
  BEAR/PANIC, `classify_regime` 由窗口数据分类)× 参数扰动(baseline/±5%/±10%)× 执行成本(0/5/10/20/30 bps)。
- 鲁棒性评分 `100 × 盈利占比 × (0.5×最坏稳健度 + 0.5×稳定性)`: 全亏 0 分「不可用」; 单次高收益不拉分;
  最坏格子深度亏损 / 跨格标准差大 → 降分。
- `RobustnessMatrixRunner` 注入 run_cell(单格异常不阻断整矩阵); `run_portfolio_cell` 接真实 `PortfolioBacktester`。
- **新增测试 21 条**(`test_v116_backtest_robustness.py`), 全量 **609/609** 通过。无新表无迁移。

### P1-2 Optimizer V2(已完成)

- 新建 `at70_backtest/backtest_optimizer.py`: 网格搜索 → Walk-Forward → 鲁棒性 → 风险调整排序,
  防「历史最优 ≠ 未来最优」过拟合。
- `build_grid` 展开参数笛卡尔积; `OptimizerV2` 注入 evaluate 回调(单点异常不阻断);
  `build_param_result` 从 train/test 收益序列派生均值/最坏/标准差/盈利占比/夏普/过拟合间隙/鲁棒性。
- 风险调整得分 = 鲁棒性 × max(0, 1+平均验证收益) × (1 - 0.5×clamp(过拟合间隙/10%)); 按此降序 `rank_params`。
- 复用 P1-1 的公共 `robustness_score`(跨窗口鲁棒性同口径)。
- **新增测试 15 条**(`test_v117_optimizer_v2.py`), 全量 **624/624** 通过。无新表无迁移。

## V11.0 深度审计 — 13 项资金正确性缺陷修复(2026-09-08)

**定位: 不再加功能, 逐行审查执行/风控/账务/行情链路, 证明「交易所/网络/进程/DB 异常下不错误改账」。**

| # | 领域 | 缺陷(简) |
|---|------|----------|
| F1 | 账务 | 已清 lot 未归零 → 交叉对账假急停 |
| F2 | 账务 | 买入费未进均价成本账 |
| F3 | 恢复 | 终态前部分成交静默丢弃 |
| F4 | 恢复 | 恢复路径记账/状态分步提交, 有重复/漏记账窗口 |
| F5 | 恢复 | 启动自愈不重放记账 |
| F6 | 执行 | 落库失败仍投交易所 |
| F7 | 部署 | init.sql 双表结构来源 + MariaDB 语法 |
| F8 | 风控 | 急停持久化静默吞错 |
| F9 | 风控 | 核心仓 REDUCE 未过 can_sell |
| F10 | 行情 | myTrades 分页缺失 |
| F11 | 恢复 | 恢复缺真实手续费 |
| F12 | 账务 | lot 无唯一约束无幂等 |
| F13 | 行情 | WS raw/agg 成交 ID 命名空间冲突 |

**新增测试 8 条**(F3/F4/F5/F8/F9/F10/F11/F12/F13 回归), 全量 **512/512** 通过。
**无新增表**; 存量库迁移见 runbook(F12: `position_lots.client_order_id` 唯一索引)。
修改 `models.py` / `market_rest_client.py`(`get_my_trades_all` 分页)/ `market_ws_client.py`(`@aggTrade`) /
`execution_executor.py` / `order_recovery.py` / `startup_reconciler.py` / `exchange_truth_reconciler.py` / `risk_lot.py`。

详见 `cc_task_v11.md`「深度审计修复记录(F1-F13)」。

## V11.0 规划 — Production Readiness(方向, 深度审计已完成, 2026-09-08)

> 外部评审(基于 commit b2dd5e2)结论: 工程完整度约 **84/100**, 实盘准备度约 **70/100**。
> 核心判断: 已从「策略原型」进入「接近实盘基础设施」阶段; 下一步「质量 > 数量」——
> 不再增加策略, 而是证明系统在交易所/网络/进程/DB/Redis/WS/订单状态异常下不会错误修改资金账本。

**P0(执行可靠性)**
- P0-1 Exchange Truth V2: myTrades 分页(fromId/startTime/endTime), 消除 limit=100 窗口假设; 恢复链路补真实手续费(commission/commissionAsset)。
- P0-2 Ledger Reconstruction: 由 Exchange Truth + Order + Fill 重建 PositionLot/SellAllocation/Position。
- P0-3 SELL Recovery: 消除 RECOVERY_REQUIRED SELL → 人工处理(FIFO 分配信息丢失)。
- P0-4 Reconciliation Engine V2: 统一 Order/Fill/Position/Lot/Cash/Equity/Ledger 的 Truth Reconciliation Matrix。

**P1(回测/风控/可观测性)**
- P1-1 Backtest V2: 30/90/180/365 天 + Walk-Forward + OOS + Monte Carlo + 参数扰动 + 滑点/手续费压力。
- P1-2 Optimizer V2: 网格搜索 → Walk-Forward + Robustness + 风险调整排序(防过拟合)。
- P1-3 System Lifecycle: INIT/WARMING_UP/SYNCING/SELF_CHECK/READY/TRADING/DEGRADED/RECOVERY/SAFE_MODE/STOPPED 顶层状态机。
- P1-4 生产可观测性: 执行延迟/对账漂移/恢复次数/订单失败率/数据缺口/策略归因 + 告警。
- P1-5 资金级 Circuit Breaker: Equity/Position/Cash 三向漂移分级处置。

**P2**: 执行/对账/策略三块 Dashboard; AI 权限架构级隔离。详见 `cc_task_v11.md`。

## V10.7 — 恢复 + 混沌工程: 7 项 P0/P1(订单事件日志 / 事件信封幂等 / 订单恢复 / 交易所真相 / RECOVERY_CHECK / 不变量 / Chaos)(2026-09-08)

**定位: 外部评审收尾第二阶段 —— 把崩溃恢复、交易所真相、急停解除、异常注入收敛补齐, 达成生产级自愈闭环。**

| 交付 | 内容 |
|------|------|
| P0-a 订单事件日志 | `execution_events` 表(append-only, event_id 非空唯一)+ `ExecutionEventLogger`, 订单生命周期全事件可追溯 |
| P0-b 事件信封幂等 | `bus.py` 事件信封(event_id/event_time/event_version/source)+ 有界内存去重, 消费不重不丢 |
| P0-c 订单恢复引擎 | `order_recovery.py`: UNKNOWN/SUBMITTING 周期收敛(交易所真相)+ RECOVERY_REQUIRED BUY 账务重建(进程内补镜像 / 重启后完整记账), SELL 保守冻结 |
| P0-d 交易所真相对账 | `exchange_truth_reconciler.py`: 本地 filled_quantity vs myTrades 成交额对账(fill_truth_missing/mismatch/orphan_trade) |
| P1-e RECOVERY_CHECK | 风险状态机新增 RECOVERY_CHECK: KILLED → reset → RECOVERY_CHECK(仍不可交易)→ confirm_recovered → NORMAL, 禁止裸 reset |
| P1-f 10 不变量 | `test_v107_invariants.py`: 幂等/持仓守恒/lot 守恒/账本守恒/记账原子性/成交覆盖/FIFO 盈亏/REDUCE_ONLY/急停持久化 |
| P1-g Chaos 测试 | `test_v107_chaos.py`: 超时/重复成交/部分成交/DB 回滚/未知订单 故障注入 + 自愈收敛断言 |

**新增表**: `execution_events`(全库 24 → 25 张)。
**新增模块**: `execution_events.py` / `order_recovery.py` / `exchange_truth_reconciler.py`。
**新测试**: test_v107_execution_events / test_v107_event_envelope / test_v107_order_recovery /
test_v107_exchange_truth / test_v107_recovery_check / test_v107_invariants / test_v107_chaos。
**验证**: 497/497 测试全绿。

**修复: 交叉对账实盘 ledger_missing 误报** —— `AccountLedger` 仅纸面模式落库(实盘
`cash_before=None` 不写账本), 但 `CrossReconciler.reconcile()` 此前只查实盘订单并核对 base
资产账本行, 导致每笔实盘成交被误判 `ledger_missing` → 误触急停冻结。修复: `reconcile()` 改为
核对实盘+纸面订单, `ledger_position` 维度仅对纸面订单核对(实盘 SOL 持仓一致性由 buy_lot/sell_alloc
+ lot 总和对账 + 权益对账兜底); 新增回归 `test_live_order_without_ledger_not_flagged` 与纸面
四维自洽测试。

## V10.6 — 生产加固: 7 项 P0/P1(ACK 语义 / 强一致记账 / 记账锁 / 幂等键 / 数量分离 / ExchangeInfo 禁 BUY / REDUCE_ONLY)(2026-09-08)

**定位: 外部评审收尾 —— 把成交后记账的强一致、幂等去重、方向闸门补齐, 消除最后几处「异常下静默漂移 / 重复摄入 / 规则未知开仓」的风险。**

| 交付 | 内容 |
|------|------|
| P0-a ACK 语义 | `bus.py` ACK 作为业务成功结果; 转投失败留 PEL + `recover_pending` 兜底(不丢不重) |
| P0-b 强一致记账 | 成交后 Position / PositionLot / SellAllocation / AccountLedger 四表单事务提交; 失败整体回滚 + 置 `orders.accounting_state=RECOVERY_REQUIRED` + 急停冻结 |
| P0-c 记账锁 | Position/Lot 记账 symbol 级 `asyncio.Lock`, 串行化核心仓并发成交的 add_buy / allocate_sell 竞态 |
| P0-d 幂等键 | `order_fills.fill_idempotency_key`(非空唯一, `订单ID:成交ID`, 缺失成交ID落 `na`), 修复原双可空唯一键的 NULL 漏洞 |
| P1-e 数量分离 | `signal.quantity` 不再被执行引擎原地改写; `exec_qty` 独立承载 REDUCE_ONLY 缩量 / 交易规则过滤调整; `signals` 落原始意图、`orders` 落实际提交量 |
| P1-f 禁 BUY | 实盘 exchangeInfo 拉取失败时 BUY 本地拒绝(不下单), SELL 减仓放行; 失败不缓存、下次自动重试 |
| P1-g REDUCE_ONLY 态 | 风险状态机新增 REDUCE_ONLY(禁开新仓/保留卖出)+ `can_buy`/`can_sell`; `RiskManager.check()`/`_on_signal`/核心仓 ADD 按方向分流 |

**新增列**: `orders.accounting_state`(VARCHAR(20) NOT NULL DEFAULT 'OK')、`order_fills.fill_idempotency_key`(VARCHAR(128) NOT NULL UNIQUE); 存量库需 ALTER + 回填(见 runbook)。
**无新增表**(全库仍 24 张); 新测试 test_v106_accounting_tx / test_v106_accounting_lock /
test_v106_fill_idempotency / test_v106_signal_exec_qty / test_v106_exchange_info_block /
test_v106_risk_reduce_only(共 22 条), P0-a 扩展 test_v105_eventbus_dlq。
**验证**: 447/447 测试全绿。

## V10.5 — 一致性加固: 5 个 P1(EventBus DLQ / ExchangeInfo / WS 回补 / REDUCE_ONLY / 风险状态机)(2026-09-07)

**定位: 停止加策略, 修交易系统最后 20% —— Order→Fill→Ledger→Position 链在异常下的自洽。**

| 交付 | 内容 |
|------|------|
| EventBus DLQ | `bus.py` 消费处理失败先重试 3 次(重入同流), 仍失败转 `<stream>:dlq` 死信队列, 不再静默丢弃; `dead_letter_count()` 可观测积压 |
| ExchangeInfo 过滤 | `exchange_filters.py` + `get_exchange_info` 按 LOT_SIZE/PRICE_FILTER/MIN_NOTIONAL 对齐 stepSize/tickSize/minQty/minNotional, 违规本地拒绝(不投交易所); 拉取失败自动降级不过滤 |
| WS 断线回补 | `market_engine.resync()` + `merge_klines/merge_trades` 幂等合并 REST 重拉快照, 刷新数据校验基线; `on_reconnect` 接线 |
| REDUCE_ONLY | 现货卖出执行前重读持仓封顶(无持仓拒绝/超仓缩量), 关掉风控审批→执行竞态; `orders.reduce_only` 标记(仅 SELL 为 1) |
| 风险状态机 | `risk_state.py` 显式 NORMAL/PAUSED/KILLED 三态, 取代隐式时间阈值暂停; 同因续期不重复告警, 进入 PAUSED 落 `risk_events(event_type='risk_state')` 审计 |

**新增列**: orders.reduce_only(存量库需 `ALTER TABLE orders ADD COLUMN reduce_only BOOLEAN DEFAULT 0`)。
**无新增表**; 新测试 test_v105_eventbus_dlq / test_v105_exchange_filters / test_v105_ws_gap_recovery /
test_v105_reduce_only / test_v105_risk_state。
**验证**: 423/423 测试全绿。

## V10.4 — 三维交叉对账(Order / Fill / Ledger / Lot 一致性)(2026-09-07)

**定位: 把「订单→成交→账本→批次」四个独立写入环节的漂移变成可检测、可冻结的硬约束。**

| 交付 | 内容 |
|------|------|
| CrossReconciler | `at50_execution/cross_reconciler.py`: 逐笔核对同一 client_order_id 在 Order / OrderFill / AccountLedger / PositionLot+SellAllocation 四维是否自洽 |
| 五类检查 | fill_coverage / fill_side / ledger_position / buy_lot / sell_alloc; 任一漂移 → 急停冻结(KillSwitch.arm + persist) |
| 接线 | run.py `_reconcile_loop` 实盘分支, 纯 DB 读(不查交易所), 窗口过滤(默认 900s / 上限 200 单) |

**无新表无迁移**; 新测试 test_v104_cross_reconcile(约 10 条)。

## V10.3 — FIFO Lot 会计(PositionLot / SellAllocation)(2026-09-07)

**定位: 在平均成本口径之上加一层 FIFO 审计, 精确逐批已实现盈亏。**

| 交付 | 内容 |
|------|------|
| Lot 会计 | `PositionLot` + `SellAllocation` + `LotTracker`: FIFO 精确已实现盈亏 + 剩余成本(不动平均成本口径); 买入费摊入 lot 成本、卖出费一次性扣 |
| 账本回写 | AccountLedger 落 realized_pnl / matched_cost |
| 对账不变量 | 开仓 lot 总和 == 持仓量; 全平仓 FIFO 累计 realized == 平均成本累计 realized; 崩溃恢复 FIFO 队列持久化 |

**新增表**: position_lots / sell_allocations; account_ledger 加 realized_pnl / matched_cost 两列(存量库需 ALTER)。
新测试 test_v103_lot_accounting。

## V10.1 / V10.2 — 生产硬化: 订单→成交→账本链 + 对账盲区(2026-09-07)

**定位: 补齐订单生命周期与对账的最后一公里 —— 状态迁移合法性、幂等摄入、反向对账。**

| 交付 | 内容 |
|------|------|
| Order→Fill→Ledger 链 | UNKNOWN/SUBMITTING 状态迁移合法性; 异常分类(4xx→REJECTED, 5xx/网络→UNKNOWN); OrderIntent DB 幂等唯一键; OrderFill 逐笔落库+幂等摄入 |
| 手续费合成 | fee_quote 合成 + AccountLedger 落 commission |
| 对账盲区 | `_execute_live` 下单前落 SUBMITTING + 每次尝试落 ExecutionAttempt(attempt_no/outcome); reconcile_live 反向检出 EXCHANGE_ONLY; startup_reconciler 对 SUBMITTING 按 clientOrderId 反查收敛 |

新测试 test_v101_order_fill_ledger / test_v102_reconciliation_execution。

## V10.0 — 实盘安全三件套(启动对账 / 权益对账 / 急停端点)(2026-09-07)

**定位: 把 V8 的「安全闸门」从「能告警」补成「能冻结 + 能恢复」——为无人值守实盘补齐最后一道硬保护。**
原则: 不新增依赖、不碰主交易链路; 冻结必须是**持久化的、人工才解除的**急停(区别于 CircuitBreaker 的 cooldown 自动复位)。

| 交付 | 内容 |
|------|------|
| 急停开关 KillSwitch | `at60_risk/risk_killswitch.py` + `kill_switch_state` 表(单行 id=1); `arm()` 幂等、`disarm()` 人工解除、`persist()`/`load_from_db()` 持久化, **重启后仍冻结** |
| 统一闸门接入 | `RiskManager.can_trade()` 首查 `kill_switch.is_armed`(优先于熔断/异常保护); `block_reason`/`status` 补急停字段 |
| 启动对账 StartupReconciler | 实盘启动时拉交易所挂单+成交历史, 对崩溃窗口做**确定性自愈**(交易所已 FILLED → 本地改 FILLED + 状态机推进); 歧义(无交易所订单ID/孤儿挂单/无法匹配)记入未解决差异 → 急停冻结 |
| 权益对账 reconcile_account | 本地权益 vs 交易所权益(计价资产 + base 资产×last_price), 超容差(默认 2%)→ 持久急停(非 60s pause) |
| exchange_order_id 落库 | `_execute_live`/`_execute_paper` 改为 5 元组返回, `_update_order_status` 落 `exchange_order_id`(启动对账可匹配) |
| 急停撤单 | `ExecutionEngine.cancel_all_open_orders(symbol)`: live 撤交易所挂单 / paper 撤本地 NEW 单, 落库 CANCELED + 状态机回退 |
| 急停/恢复端点 | `POST /api/emergency/kill`(冻结+撤单+持久化+记事件)、`POST /api/emergency/recover`(解除+持久化+记事件) |
| 配置 | `startup_reconcile_enabled` / `equity_reconcile_tolerance_pct`; run.py 启动接线(实盘才对账)+ `_reconcile_loop` 周期权益对账 |

**新增表**: kill_switch_state(全库 18 → 19 张)。
**新增配置**: startup_reconcile_enabled / equity_reconcile_tolerance_pct。
**验证**: 340/340 测试(313 → +27); 新增 test_v10_killswitch / test_v10_reconciliation / test_v10_emergency_api。

**冻结不变**: 零新依赖; 纸面模式不查交易所(启动对账仅实盘); 急停不自动复位, 只能人工 recover。

## V9.0 — SOL Adaptive Swing Trader: 记忆交易实验平台 M1(2026-09-07)

**定位: 不是加策略, 而是把系统升级为「有记忆的交易实验平台」——沉淀每次判断/交易/环境/盈亏原因, 供 AI 未来 6-12 个月优化。**

原则: Binance 单所 + SOLUSDT 单币 + 双仓 + 低频; AI 只优化不交易; 每步可回滚、兼容 paper、加测试加日志。

| 交付 | 内容 |
|------|------|
| Portfolio Manager | `at55_portfolio/` 薄编排层: 核心/交易/现金三桶(config 驱动, 替代硬编码 70/30) |
| Core Position Manager | ADD/REDUCE/HOLD + Trend Break Protection(EMA 死叉 / BTC 锚失败 / PANIC) |
| Trading Journal | `trade_records` 表: 成交闭环 entry/exit/profit/holding/max_profit/max_drawdown |
| Strategy Version | `strategy_versions` 表: 参数快照(不可变, 供回测-实盘对比) |
| 统一闸门 | `RiskManager.can_trade()` 合并熔断/异常保护, `_on_signal` 与核心仓决策短路 |
| 每日复盘 | `reports/YYYY-MM-DD.md` 自动生成(决策/成交/绩效) |

**新增表**: trade_records / strategy_versions(全库 15 → 17 张); position_bucket 追加 target 三列。
**新增配置**: portfolio_core/trading/cash_ratio、portfolio_rebalance_interval_seconds、daily_report_enabled 等。
**验证**: 235/235 测试(单元 215 + 集成 20); 新增 test_v9_portfolio / test_v9_journal / test_v9_strategy_version。

## V9.0 — M2: Regime 6 态 + 策略整合 + 回测指标 + at80_optimizer(2026-09-07)

**前置目的: 让 at80_optimizer 能跑起来 —— 可量化回测指标作目标函数、strategy_versions 作实验台账、统一策略分组作优化单位。**

| 交付 | 内容 |
|------|------|
| Regime 6 态 | 中性区三档: NORMAL <1.0% / SIDEWAY 1.0~1.5% / VOLATILE ≥1.5%; BULL/BEAR/PANIC 判定不变, 6 张系数表补键 |
| 策略整合(薄分组层) | `strategy_group.py` 3 伞: Trend Swing(trend+entry) / Mean Reversion(grid+entry) / Exit Manager(exit); 归因统一到伞名 |
| Exit Manager 统一 | 分批止盈阶梯 settings 化(`sell_take_profit_ladder`) |
| 回测指标 6 项 | win_rate / profit_factor / holding / sortino / calmar / attribution; 闭环成交跟踪 |
| AI 优化器 | `at80_optimizer/`: 候选生成 → 回测评估 → 落库 → 排序提案(**不自动 activate**) |

**验证**: 280/280 测试(M1 235 → +45); 新增 test_v9_regime_6state / test_v9_strategy_group /
test_v9_backtest_metrics / test_v9_optimizer。

**修复已知不一致**: `trade_records` 与 `strategy_performance` 两表归因首次统一(此前一记 source_strategy、
一记 "decision")。

## V9.0 — M3: 审查落地(README 对齐 + 账本审计 + 条件滑点 + HMM/Funding 可选模块)(2026-09-07)

**前置: 外部评审(7.6/10)逐条对照后, 大部分建议已落地; 本里程碑补齐 5 项真正未落地且有价值者。**

| 交付 | 内容 |
|------|------|
| README 对齐 V9.0 | 修 "V2.0" 冻结标题、目录表补 at40_journal/at55_portfolio/at80_optimizer、6 态 Regime + 3 策略伞、回测 6 指标、测试数 303、`cc_task.md` → `cc_task_v9.md` |
| account_ledger 审计账本 | `account_ledger` 表 + `AccountLedgerWriter`: 每笔成交落 USDT + SOL 两行 before/change/after, 落库失败降级不打断成交 |
| Regime 条件滑点 | `SlippageModel(regime_bps)` PANIC/VOLATILE/BEAR 放大; intent 透传 regime; `slippage_regime_bps` settings |
| HMM Regime(可选) | `regime_hmm.py` 纯 Python 对角协方差高斯 HMM(log 域前向-后向 + Viterbi); `regime_hmm_train.py` 离线训练; 默认关闭不接实盘 |
| Funding + OI 情绪(可选) | `market_futures_client.py` + `sentiment.py` 合成情绪分; 默认关闭, run.py 低频轮询挂起 |

**新增表**: account_ledger(全库 17 → 18 张)。
**新增配置**: slippage_regime_bps / regime_hmm_enabled / regime_hmm_model_path / sentiment_enabled / sentiment_poll_interval_seconds / sentiment_funding_threshold / binance_futures_base_url。
**验证**: 303/303 测试(M2 280 → +23); 新增 test_v9_account_ledger / test_v9_regime_slippage / test_v9_regime_hmm / test_v9_sentiment。

**冻结不变**: HMM / Funding-OI 默认关闭不接实盘 regime; 零新依赖(纯 Python); 单所单币双仓低频。

## V9.0 — AI 供应商化(2026-09-07)

**前置: 复用 bianAgent `src/config/llm_config.py` 的供应商选择模式, 将 AI 顾问改造为供应商可切换、Key 全部入 .env。**

| 交付 | 内容 |
|------|------|
| AI 供应商配置中心 | `at50_strategy/llm_config.py`: `LLMConfig(BaseSettings)` + `get_llm_config()` + `resolve_provider()`, 支持 openai/qwen/deepseek 三供应商, Key 从 `.env` 读、不硬编码 |
| AIAdvisor 供应商化 | `strategy_ai_advisor.py` 改用 `resolve_provider`, 统一走 OpenAI Chat Completions 兼容协议 |
| 配置 | `ai_provider` 作供应商选择参数(默认 deepseek); `ai_base_url`/`ai_api_key` 改为通用覆盖(空则用供应商默认); `.env` 迁移 QWEN/DEEPSEEK/OPENAI Key |

**验证**: 313/313 测试通过; 新增 `test_v9_ai_provider`(供应商解析/未知禁用/Key 缺失禁用)。

**冻结不变**: 零新依赖(不引入 langchain, 复用 aiohttp 原生协议); AI 只建议不交易。

## V8.0 — 生产加固与账务修复(2026-09-07)

**原则: 单一记账、状态可持久化、重启可对账、主网有安全闸门。**

针对 `at50_execution/*` / `run.py` / `database/persistence` 的审查, 修复 13 项问题
(4 P0 + 4 P1 + 5 P2):

| 层级 | 要点 |
|------|------|
| P0 账务 | 消除成交双重记账(Bucket 变纯拆分跟踪, 总账只经 PortfolioEngine); PortfolioEngine 正确接线; 状态机死代码打通; PARTIALLY_FILLED 处理 |
| P1 持久化 | 交易状态机 + PaperBroker 现金落库; 交易所持仓对账(PositionReconciler); 行情校验(MarketDataValidator) |
| P2 安全 | AI 降频 86400s + 参数审批层; Kill Switch 补快速崩盘/余额不匹配; 日志轮转; 主网实盘二次确认守卫 |

**顺带修复**: 回测卡死(回测未建表 → `init_db()` 兜底)+ 补装 `aiosqlite` 测试依赖。

**验证**: 219/219 测试(单元 199 + 集成 20)。新增模块 data_validator / reconciliation / ai_parameter_guard;
新增表 trade_state / paper_state; 新增配置 live_trading_confirm / reconcile_interval_seconds。

## 状态总览(按评估框架)

```
① Accounting      ✅ V6(账本+对账不变量)+ V8(单一记账固化)
② Backtest真实化   ✅ V7(真实策略管线+滑点+次bar)
③ Test/Invariant  ✅ 219 用例四层
④ No Lookahead    ✅ V7(次bar执行)
⑤ Attribution     ← P1 下一项
⑥ Walk Forward    ← P1
⑦ AI 自动调参      ✅ V8(审批层已落地, 待 A/B 验证)
```

**结论: 暂不建议实盘** —— 策略跑输持有(V7 结论), 但 V8 已补齐无人值守所需的
正确性/持久化/对账/安全闸门, 纸面可安全长跑。

## V7.0 — Backtest Realism 回测真实化(2026-09-07, commit 2eba4f7)

**原则: 回测里的策略, 必须就是实盘里的策略。**

V6 审查发现的结构性问题(修了表面、留下深层问题)并全部修复:

| 问题 | 修复 |
|------|------|
| 回测内嵌 "+3%/-3%/15%" 隐藏交易策略 | 删除, 回测驱动真实 StrategyEngine |
| Decision 直接决定交易数量 | 降级为建议参考值, 数量由 Sizer 决定 |
| unknown strategy 静默 0.5 权重 | 警告并跳过计票 |
| 无滑点模型 | SlippageModel(0/5/10/20bps 敏感性) |
| candle close 同时判断+成交(look-ahead) | NextBarExecutor 次bar执行 |
| BTC 精确 timestamp match | AsOfJoiner asof 对齐+数据龄 |
| interval 分页浪费 | timeframe.py 统一换算 |

**验证**: 217/217 测试; 真实 SOL 滑点敏感性:
0bps +0.18% / 10bps +0.01% / 20bps -0.21%

**关键洞察(诚实结论)**: 48+ 笔交易在 10bps 滑点下吃掉全部利润——
高频网格摩擦成本是最大亏损源; 交易仓在上涨段被网格卖出无法吃到趋势。
P1 参数优化(网格频率/Attribution)有了量化目标。

## V6.0 — Correctness First(2026-09-07, commit 9f76b03)

**原则: 不新增功能, 修复正确性。** 代码审查确认 8 个真实 Bug 并全部修复:

| Bug | 影响 |
|-----|------|
| 策略名 buy/sell ≠ 权重键 entry/exit | entry=1.0/exit=1.3 权重失效, 掉 0.5 默认值 |
| 融合信号 strategy='buy+decision' | on_fill 回调静默丢失 |
| 回测 trade PnL 用 core_cost | 交易仓盈利严重虚高(170-100 vs 170-150) |
| 回测交易仓初始化为 pass | 双仓模型从未完整运行 |
| 回测风险因子固定 btc=0/neutral/drawdown=0 | Live ≠ Backtest 风险模型 |
| 历史数据 1000 根上限 | "7天回测"实际只有 16.7 小时 |
| 回测自写简化 Regime / Sharpe 固定 1m 年化 | 数据可信度问题 |

**交付**: StrategyType 枚举统一 / Signal.source_strategy 回调路由 /
PortfolioLedger(双仓独立成本+对账 reconcile API)/ 交易仓真实生命周期 /
分页历史数据 / BTC 对齐风险因子 / 共用 MarketRegimeEngine /
金融正确性测试四层(Invariant/Accounting/Regression)

**验证**: 202/202 测试; 回测对账恒平衡; 真实 3 天 4320 根 BTC 对齐:
core +147.18 / trade -27.32 / 17 次再平衡 / 37 笔

**诚实结论**: 修正账目后, 3 天策略跑输 Buy-Hold 1.57%(此前账目虚高)。
交易仓高抛低吸在上涨段提前卖出是主要亏损源 —— 这是 P1 参数优化
(阈值敏感性/止盈阶梯)的真实起点, 而非继续堆功能。

## V3.0 — 交易决策层与信号闭环(2026-09-06, commit 062f7fa)

**目标**: 从"能运行的交易机器人"升级为"可长期迭代的量化交易平台"

### 交付
| 模块 | 内容 |
|------|------|
| Decision Engine | 多策略加权融合 → 唯一 BUY/SELL/HOLD(exit 1.3 保命权重, regime 系数, 高分退出优先) |
| Portfolio Engine | 成本管理: 卖出降本/保本价/成本曲线/目标仓位/再平衡 |
| signal_result | 信号未来收益逐分钟跟踪(1h 窗口), 供调权与 AI 学习 |
| Walk-Forward 回测 | 滚动窗口+阈值网格调优+过拟合间隙检测 |
| 交易/订单状态机 | IDLE→ENTRY_PENDING→HOLDING→EXIT_PENDING→CLOSED, 防重复建仓 |
| Exit 标签 | profit_target / overbought / trend_reverse / risk_reduce |
| Alpha Engine | 综合评分(5 因子, 含 SOL/BTC 相对强弱) |
| database 惰性引擎 | reset_engine 支持测试隔离 |

### 验证
- 测试 **140/140**(新增 31 个 V3 测试)
- E2E(真实 SOL 行情): 融合决策 BUY(net 0.42)→成交→EXIT_PENDING→CLOSED→IDLE 零非法迁移;
  signal_result 实时落库(grid+decision BUY profit 追踪中)
- 修复: enum 类内 dict 被成员化(迁移表外置)、状态机推进时序(移到持仓更新后)、
  纸面模式中间态补齐、Portfolio 变量名、V2 幂等测试语义更新(HOLDING 拦截重复买入为正确行为)

## V2.0 — SOL 专业量化系统(2026-09-06, commit 505010f)

**目标**: 评分化策略 + 市场环境 + 百分比风控

### 交付
- Entry 评分模型(5 维加权, ≥80 买 / 60-80 观察 / <60 禁止)
- Exit 分批止盈(5%→20%/10%→30%/20%→50%) + 移动止盈(5%) + 趋势退出
- Market Regime Engine(BULL/SIDEWAY/BEAR/PANIC)
- 百分比风控(仓位 40%/单笔 5%/日亏 5%/回撤 15%) + 异常保护
- 幂等执行 + strategy_performance + 持仓快照
- 回测引擎 v1 / Redis Stream 事件总线 / Dashboard 增强
- 标准信号: score 0~100 + reason 列表 + indicators 快照落库

### 验证
- 测试 109/109; E2E: Entry 65 分正确进入观察档被拦截; 网格+Exit 全链路

## 结构重组(commit f87e5e9)

- 目录重命名 atXX 前缀(at01_common ~ at90_deploy), 拼写修正(ayalytics→analytics, startegy→strategy)
- 全部包扁平化: **目录即包名**, 模块文件带前缀(market_engine.py / strategy_base.py ...)
- at10_web 分层: web_app / web_api_routes / web_ws_stream / web_state / web_serve_standalone(前端独立启动)
- 39 文件全局 import 重写, git rename 历史保留

## V1.0 — 全链路实现(2026-09-06, commit 5029ca0)

- 行情(WS 组合流+自动重连+预热)→分析(VWAP/CVD/Whale/吸筹)→策略→风控→执行(纸面)→Web
- 7 张表 ORM + Docker 部署(MySQL/Redis/compose) + 监控面板
- 测试 75/75; 真实币安测试网 BTC 端到端验证

## 待办(P2)

- [ ] AI 参数建议自动应用到策略(当前仅展示)
- [ ] 多币种并行(架构已支持, 需 BTC 锚订阅与 per-symbol 状态机验证)
- [ ] Funding Rate 情绪因子(需合约 API)
- [ ] 链上大额转账监控(交易所流入/流出)
- [ ] 策略市场化(参数云端配置热加载)
