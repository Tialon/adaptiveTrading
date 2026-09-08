# adaptiveTrading V11.0 — Production Readiness

> 定位: 不是增加赚钱策略, 而是证明——即使交易所 / 网络 / 进程 / 数据库 / Redis / WebSocket /
> 订单状态发生异常, 系统仍不会错误地修改资金账本。
> 原则: 质量 > 数量; 不再新增 RSI/MACD/Bollinger/AI/ML/Transformer/RL 等策略。
> 冻结不变: Binance 单所 / SOLUSDT 单币 / 双仓 / 低频; AI 只分析优化、不直接下单。

## 评审基线(基于 commit b2dd5e2)

- 综合: 工程完整度约 **84/100**; 实盘准备度约 **70/100**。
- 结论: 核心架构已成型, 瓶颈从「有没有功能」转向「有没有证据证明异常下不出现资金级错误」。
- 方向: 停止堆功能, 进入「证明正确性」阶段。

## 深度审计修复记录(F1-F13, 已全部完成)

> 按「启动前置」六项审查范围(`at50_execution` / `at60_risk` / `models.py` / `run.py` /
> 对账器 / `init.sql` + 全部测试)逐行深度审查, 定位并修复 13 处资金正确性缺陷(F1-F13)。
> 每项附回归测试; 全量测试 **512/512** 通过。commit 跨度 `1d30086` → `b69afe6`。

### 账务 / 批次(F1 / F2 / F12)

| # | 严重度 | 缺陷 | 修复 |
|---|--------|------|------|
| F1 | P0 | 已清 lot 落库仅置 `status=closed` 未归零 `quantity`, 交叉对账按「剩余+已卖出」反推买入量时把残留 quantity 当成剩余, 误判 `buy_lot_mismatch` → 假急停 | 关闭时 quantity 归零; 交叉对账 closed lot 计 0 剩余 |
| F2 | P1 | 买入手续费未透传进均价成本账(`on_buy_fill` 丢 fee), 与 FIFO lot「摊入买入费」口径不一致 → 两套成本漂移 | `on_buy_fill` 透传 fee, 均价成本与 lot 单位成本对齐 |
| F12 | P1 | `PositionLot.client_order_id` 无唯一约束且 `_insert_lot` 无幂等, 崩溃窗口恢复重放可重复落 lot | 加 `unique=True` + `_insert_lot` 复用既有 lot(幂等) |

### 订单生命周期 / 恢复(F3 / F4 / F5 / F6)

| # | 严重度 | 缺陷 | 修复 |
|---|--------|------|------|
| F3 | P0 | 终态前部分成交(`executedQty>0` 的 CANCELED/REJECTED/EXPIRED)被静默丢弃不记账 → 持仓/权益永久漂移 | 恢复路径先记账部分成交再落终态(`final_status="CANCELED"`) |
| F4 | P0 | 恢复路径「记账」与「状态/`accounting_state=RECOVERED`」分两步提交, 崩溃后存在重复记账 / 漏记账窗口 | 记账 + 状态 + accounting_state 同一事务原子落; 幂等 skip 守卫 |
| F5 | P0 | 启动自愈 FILLED 只改订单状态 + 推进状态机, 不重放成交记账 → 崩溃窗口「交易所已成交但本地持仓/lot 未落」永久漂移 | 复用 `apply_recovered_fill` 完整记账(内存 + DB 镜像) |
| F6 | P0 | 下单/幂等落库失败仍投交易所 → 交易所已成交但本地无记录(漏记账路径) | 落库失败 fail-closed, 不投交易所 |

### 风控闸门(F8 / F9)

| # | 严重度 | 缺陷 | 修复 |
|---|--------|------|------|
| F8 | P1 | `KillSwitch.persist()` 静默吞错, 急停态持久化失败无感知(重启后可能丢急停) | 重试 3 次 + 返回 bool, 调用方据此判定 |
| F9 | P1 | 核心仓 REDUCE 未过 `can_sell()` 闸门, 急停/REDUCE_ONLY 下仍可减仓 | `_apply_core_action` 对 REDUCE 加 `can_sell()` 闸门 |

### 行情 / 成交数据(F7 / F10 / F11 / F13)

| # | 严重度 | 缺陷 | 修复 |
|---|--------|------|------|
| F7 | P1 | `init.sql` 建表 + ORM `create_all` 双表结构来源, 且含 MariaDB-only 语法(schema 漂移) | `init.sql` 收敛为仅建库, `create_all` 唯一表结构来源 |
| F10 | P1 | myTrades 仅 `limit=100`, 依赖「15 分钟 ≤100 笔成交」假设, 高频时截断漏单 | 新增 startTime/endTime/fromId 分页 + `get_my_trades_all`, 对账走分页 |
| F11 | P1 | 恢复链路 `fee=0` 缺真实手续费, 大单多笔成交(>50)被默认 limit 截断 | 恢复路径从 myTrades 重摄取真实手续费; `_ingest_fills` limit 提到 1000 |
| F13 | P1 | WS raw trade ID('t')与 REST aggTrade ID('a')不同命名空间, 去重/唯一键冲突 | WS 成交流 `@trade` → `@aggTrade`, 与 REST 口径统一 |

### 回归测试清单(新增 8 条)

- `test_v107_order_recovery.py`: F3 `test_canceled_partial_fill_accounts` / F4 `test_recovered_fill_atomic_status_and_accounting` / F11 `test_recovered_fill_uses_trade_fee`
- `test_v102_reconciliation_execution.py`: F5 `test_startup_self_heal_applies_accounting`
- `test_v103_lot_accounting.py`: F12 `test_duplicate_client_order_id_reuses_lot`
- `test_v110_market_consistency.py`: F13 `test_subscribes_agg_trade_not_raw` / F10 `test_pages_until_short_page` + `test_empty_stops_immediately`
- `test_v10_killswitch.py`: F8 `test_persist_returns_true_on_success` / F9 `test_armed_rejects_sell_signal` + `test_reduce_only_allows_sell_not_buy`

## P0 — 执行可靠性(下一轮最优先)

| # | 任务 | 现状缺口 | 目标 |
|---|------|---------|------|
| P0-1 | Exchange Truth V2 | myTrades 仅 `limit=100`, 依赖「15 分钟 ≤100 笔成交」假设; 恢复链路 `fee=0` 缺手续费 | 分页 fromId / startTime / endTime 覆盖完整成交; 恢复用 commission / commissionAsset 重建 Fill |
| P0-2 | Ledger Reconstruction | PositionLot / SellAllocation 丢失后无法从交易所真相重建 | Exchange → Orders → Trades → Buy Lots → Sell Allocation → Position → Portfolio → Equity 全链重建 |
| P0-3 | SELL Recovery | RECOVERY_REQUIRED SELL 仅人工冻结(FIFO 分配信息丢失) | 消除人工处理, SELL 自动自愈 |
| P0-4 | Reconciliation Engine V2 | 各对账器分散, 无统一矩阵 | Order / Fill / Position / Lot / Cash / Equity / Ledger 的 Truth Reconciliation Matrix |

## P1 — 回测 / 风控 / 可观测性

| # | 任务 | 说明 |
|---|------|------|
| P1-1 | Backtest V2 | 时间 7/30/90/180/365 天 × 市场态(BULL/NORMAL/SIDEWAY/VOLATILE/BEAR/PANIC)× 参数(baseline/±5%/±10%)× 执行(0/5/10/20/30bps)矩阵; 输出 Robustness 而非单一 return |
| P1-2 | Optimizer V2 | 网格搜索 → Walk-Forward → Robustness → 风险调整排序; 防「历史最优 ≠ 未来最优」过拟合 |
| P1-3 | System Lifecycle | 顶层状态机 INIT/WARMING_UP/SYNCING/SELF_CHECK/READY/TRADING/DEGRADED/RECOVERY/SAFE_MODE/STOPPED; 与 RiskState + 连接状态 + 对账状态共同决定 CanTrade |
| P1-4 | 生产可观测性 | 执行延迟 / 对账漂移 / 恢复次数 / 订单失败率 / 数据缺口 / 策略归因 + 告警 |
| P1-5 | 资金级 Circuit Breaker | Equity / Position / Cash 三向漂移分级处置(0.1%/0.2%/0.5%); Position / Cash 漂移 → REDUCE_ONLY |

### P1-1 Backtest V2 ✅(2026-09-08)

- `at70_backtest/backtest_robustness.py`(新建): 四维鲁棒性矩阵, 用「鲁棒性评分」取代「单一收益」作为上线判据。
- 矩阵维度(全组合 5×6×5×5 = 750 格): 时间窗口(7/30/90/180/365 天)× 市场态(BULL/NORMAL/SIDEWAY/
  VOLATILE/BEAR/PANIC, 由窗口数据 `classify_regime` 分类, 非强制)× 参数扰动(baseline/±5%/±10%,
  缩放 rebalance_tolerance)× 执行成本(0/5/10/20/30 bps 滑点)。
- 鲁棒性评分 `score = 100 × 盈利占比 × (0.5×最坏稳健度 + 0.5×稳定性)`: 全亏直接 0 分「不可用」;
  单次高收益不拉分(盈利占比是乘数); 最坏格子深度亏损或跨格标准差大 → 降分。
- `RobustnessMatrixRunner`(注入 run_cell, 单格异常不阻断整矩阵)+ `compute_robustness` 纯函数 +
  `run_portfolio_cell` 接真实 `PortfolioBacktester`(cost_bps→slippage, param_pert→rebalance_tolerance)。
- **回归测试 21 条**(`test_v116_backtest_robustness.py`): 矩阵枚举 750 格 / 市场态分类 7 态 /
  鲁棒性评分(全盈/全亏/单次高收益惩罚/中位数抗离群/regime 分组/成本单调退化/参数分组)+ 编排器容错。
  全量 **609/609** 通过。
- 无新表无迁移(纯回测分析层, 不进实盘交易循环)。

## P2

| # | 任务 |
|---|------|
| P2-1 | 执行 Dashboard(订单/REST/WS/Fill 延迟、拒绝率、重试、UNKNOWN、恢复订单) |
| P2-2 | 对账 Dashboard(Position/Cash/Order/Fill/Ledger 漂移 + 最近成功对账时间) |
| P2-3 | 策略 Dashboard(Signal→Order→Fill→PnL→Strategy 归因) |
| P2-4 | AI 权限架构级隔离(at50_strategy 禁止 import execution) |

## 验证目标(启动时补)

- [x] 全量测试回归通过(当前 609/609)
- [ ] 画出 Order → Fill → Lot → Position → Ledger → Equity 完整资金守恒链(由 cross_reconciler + 不变量测试覆盖)
- [x] 找出所有「重复下单 / 重复记账 / 漏记账 / 错误急停 / 错误恢复 / 资金漂移」路径 → 见「深度审计修复记录 F1-F13」

## V11.1 — Financial Correctness & Self-Healing(进行中)

> 定位不变(证明异常下不错误改账)。本轮补上五大资金正确性闭环, 分级 P0/P1。
> 冻结不变: Binance 单所 / SOLUSDT 单币 / 双仓 / 低频; AI 只分析优化、不直接下单。

### P0-1 Exchange Truth V2 ✅(2026-09-08)

- `at20_market/market_rest_client.py`: `get_my_trades_all` 返回 `MyTradesResult`(去重 + `duplicate_ids` / `gaps` / `pagination_exhausted` / `complete`), 翻满 `max_pages` 仍见满页 → 标记截断。
- `at50_execution/exchange_truth_reconciler.py`: 真相对账窗口从本地订单 `created_at` 推导(减 60s buffer), 去掉硬编码「15min / 200 笔」; 分页耗尽返回 `truth_incomplete` / `pagination_exhausted`, 数据不完整时**跳过**逐订单成交核对与孤儿检测(避免误判 `fill_truth_missing` / `fill_truth_mismatch` / `orphan_trade`)。
- `at50_execution/startup_reconciler.py`: 解包 `MyTradesResult.trades`(兼容旧 list)。
- `run.py`: 对账差异分级 —— `truth_incomplete` / `pagination_exhausted` → `pause`(降级不冻结); `trade_duplicate` / `trade_id_gap` → 仅可观测性告警; 仅 `fill_truth_missing` / `fill_truth_mismatch` / `orphan_trade` 才 `kill_switch.arm`。
- **回归测试 9 条**: `test_v110_market_consistency.py`(分页耗尽 / 重复去重 / 跳号)+ `test_v111_exchange_truth_v2.py`(不完整跳过成交核对 / 重复去重不改账 / 跳号不影响核对 / 窗口从订单时间推导 / 一致无差异 / 不完整抑制孤儿误报)。全量 **521/521** 通过。

### P0-2 Fee Accounting Contract ✅(2026-09-08)

- `at50_execution/fee_calculator.py`(新建): 统一 `FeeCalculator`(USDT=quote / SOL=base 可折算; 其它如 BNB → `unpriced` 降级, **不静默 fee=0**)+ `FillFee` / `FeeResult` 数据类。
- `at50_execution/execution_executor.py`: `_compute_fill_metrics` 返回 `(avg, fee_quote, fee_unpriced)`; `_record_fills` 逐笔落 `fee_quote` / `fee_valuation_status`; `_ingest_fills` 检测到不可计价手续费 → 告警 + `risk.pause` 降级。
- `at01_common/models.py`: `OrderFill` 新增 `fee_quote` + `fee_valuation_status(zero/priced/unpriced)` 两列。
- **回归测试 10 条**(`test_v112_fee_accounting.py`): 计价器 4 态 / 汇总 unpriced 标记 / 成交指标标记 / 落库 status / 摄入降级(pause)vs 可计价不降级。全量 **531/531** 通过。
- 迁移: 存量库 `order_fills` 需补两列(见 runbook)。

### P0-3 Ledger Reconstruction Engine ✅(2026-09-08)

- `at50_execution/ledger_reconstruction.py`(新建): 从交易所真相(myTrades 全量成交)重建账务状态,
  链 `Exchange Truth → Trades(OrderFill) → Orders(订单分组) → Buy Lots(PositionLot) →
  Sell Allocations(SellAllocation) → Position → Cash → Ledger → Equity`。
- **幂等**: 纯函数 `build_plan` 同输入同输出; `apply` 单事务「先清后插」可重复执行终态一致。
- **dry-run + apply**: `reconstruct(dry_run=True)` 只产出计划不落库; `False` 且无歧义才落库。
- **守恒检查**: base 守恒 / 无超卖 / 卖出分配覆盖 / 现金守恒(有现金锚时), 任一失败 → SAFE_MODE。
- **SAFE_MODE(歧义拒绝 apply)**: 成交历史不完整(分页截断) / 缺现金锚 / 超卖 / 同订单成交方向不一致 /
  无交易所客户端。现金锚缺失即 SAFE_MODE(交易所成交史只有现金变动, 不猜绝对现金)。
- **口径**: 不可计价手续费(BNB)不触发 SAFE_MODE(USDT 现金守恒不受影响), 但权益置 None;
  只重建 OrderFill/PositionLot/SellAllocation/Position, `orders`(client_order_id 本地幂等键不可重建)
  与 `account_ledger`(append-only 审计)不重写。
- **回归测试 12 条**(`test_v113_ledger_reconstruction.py`): 持仓/lot/分配重建 + FIFO 盈亏 + 现金/账本守恒 +
  4 类 SAFE_MODE + 幂等 + dry-run/apply。全量 **543/543** 通过。

### P0-4 SELL Recovery ✅(2026-09-08)

- `at50_execution/execution_executor.py`: 新增 `rebuild_sell_accounting` —— RECOVERY_REQUIRED SELL
  从 DB 开仓 `PositionLot`(权威未消费态)确定性重放 FIFO 分配, 单事务落 `SellAllocation` +
  减 lot + 更新 `Position`(平均成本口径: 卖出不改 avg_price、清仓归零)+ `Order`(FILLED +
  RECOVERED), 完成后 `_resync_sell_memory` 重同步内存持仓 / FIFO lot 队列。
- 关键: SELL 与 BUY 不同 —— 记账失败时 in-memory lot 已被 `allocate_sell` 消费(内存先改、
  DB 事务整体回滚), 故不能复用 `apply_recovered_fill`(会对已分歧的内存二次消费)。
  从 DB 重放使「进程内失败 / 重启后」两场景收敛一致(DB 始终是回滚后的卖出前权威快照)。
- `at50_execution/order_recovery.py`: `_recover_accounting` 对 SELL 走 `_recover_sell_accounting`
  (本地成交数据缺失时从交易所真相补齐), 移除「SELL 保守冻结交人工」。
- **回归测试 7 条**(`test_v114_sell_recovery.py`): FIFO 分配重建 + 平均成本盈亏 + 清仓归零 +
  幂等 + 分歧内存重同步 + 交易所补齐 + 真实手续费 + 超卖截断; 并更新 `test_v107_order_recovery.py`
  的 SELL 冻结用例为自愈用例。全量 **550/550** 通过。
- 无新表无迁移(复用 `PositionLot` / `SellAllocation` / `Position` / `Order`)。

### P0-5 Reconciliation Matrix ✅(2026-09-08)

- `at50_execution/reconciliation_matrix.py`(新建): 统一四态判定 `Severity`(PASS/DEGRADED/RECOVERY_REQUIRED/KILLED)+
  `Finding`/`Verdict`/`ReconciliationMatrix`。消除各对账器「分散、各自独立 arm kill」现状, 收敛为单一 kill 决策点。
- **核心规则「单一对账器不得 kill」**: 跨源资金级差异(`equity_drift`/`orphan_trade`/`exchange_only`/
  `fill_truth_missing`/`fill_truth_mismatch`, 本地 vs 交易所天然交叉验证)单源即 KILLED; 本地 DB 内部一致性破坏
  (`fill_missing`/`fill_mismatch`/`fill_side_mismatch`/`ledger_missing`/`ledger_position_mismatch`/
  `buy_lot_mismatch`/`sell_alloc_mismatch`/`lot_sum_mismatch`)单一对账器只 RECOVERY_REQUIRED(先自愈不 kill),
  仅当 ≥2 独立对账器同周期共同报告才升级 KILLED; `recover_unresolved` → RECOVERY_REQUIRED;
  数据不完整/现金异常 → DEGRADED(pause 自动恢复); `api_error`/`trade_duplicate`/`trade_id_gap` 等 → PASS 仅记录。
- `run.py`: `_reconcile_loop` 重构为「摄入各对账器 findings → `matrix.verdict()` → `_apply_verdict`」,
  新增 `_apply_verdict`(PASS 无动作 / DEGRADED·RECOVERY_REQUIRED pause / KILLED arm+persist)。
- **回归测试 38 条**(`test_v115_reconciliation_matrix.py`): 档位映射 + 空/可观测性/降级/需恢复/跨源单源 kill/
  单一内部不 kill/双对账器佐证 kill/同对账器多报不 kill/KILLED 优先于降级/matrix ingest·reset。全量 **588/588** 通过。
- 无新表无迁移(纯判定层)。

### P0 全部完成 ✅

P0-1 ~ P0-5 五大资金正确性闭环全部落地(见上); P1 项见「P1 — 回测 / 风控 / 可观测性」表。

## 启动前置(评审建议的下一轮深度审查)

1. 审查 `at50_execution` 全部执行代码
2. 审查 `at60_risk` 全部风控代码
3. 审查 `at01_common/models.py` 全部账务模型
4. 审查 `run.py` 主循环
5. 对照 CrossReconciler / PositionReconciler / ExchangeTruthReconciler / OrderRecovery
6. 对照 `init.sql` 与全部 `tests/unit + tests/integration`
