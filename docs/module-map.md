# 模块清单(代码地图)

> 目录即包名,文件名带模块前缀。检索代码从这里出发。
> 当前状态: 1236 测试 / 回测=实盘同一策略代码 / 对账恒平衡 / V11.0 深度审计 13 项资金正确性缺陷(F1-F13)全部修复(记账原子性 / 成交分页 / lot 幂等 / 成交流口径统一) / V11.1 P0-1 Exchange Truth V2(myTrades 分页完整性检测 + 降级不冻结) / V11.1 P0-2 Fee Accounting(统一 FeeCalculator, 不可计价手续费降级不静默 fee=0) / V11.1 P0-3 Ledger Reconstruction(交易所真相重建账务 + 守恒检查 + SAFE_MODE) / V11.1 P0-4 SELL Recovery(RECOVERY_REQUIRED SELL 从 DB lot 确定性重放, 消除人工冻结) / V11.1 P0-5 Reconciliation Matrix(统一四态判定 + 单一对账器不得 kill) / V11.1 P1-1 Backtest V2(四维鲁棒性矩阵 + 鲁棒性评分取代单一收益) / V11.1 P1-2 Optimizer V2(网格搜索→Walk-Forward→鲁棒性→风险调整排序, 防过拟合) / V11.1 P1-3 System Lifecycle(顶层状态机 + 四维 CanTrade 闸门) / V11.1 P1-4 生产可观测性(MetricsStore + 阈值告警 + 策略归因) / V11.1 P1-5 资金级 Circuit Breaker(Equity/Position/Cash 三向漂移分级 0.1%/0.2%/0.5%) / V11.2 集成层(TradingGate 六维闸门 + SystemLifecycle 10 态 + 资金级熔断执行链 + 对账矩阵) / V11.3 生产加固(单币冻结 / Settings fail-fast / Recovery 重启语义 / 任务泄漏 / DB 一致性 / 手续费最终审计 / 可观测性加固 / 主网守卫 / 提案守约) / V11.4 运行时验证(长跑 soak + 异常绝不 BUY + 恢复状态机穷举 + run.py 监督审计 + 运行报告 + 死指标消除 + CI ruff/coverage) / V11.5 运维加固(Web 安全 + RuntimeSupervisor + 运行时健康快照 + 故障注入 + 类型/静态审计 + 依赖/供应链) / V11.6 测试网验证 + 财务真相闭环(BUY 安全契约 + AccountLedger 实盘边界 + 运行时健康契约 + 前向迁移框架 + soak runner + run.py 瘦身) / V11.7 测试网证据 + 运维加固(状态模型 + soak 优雅停机/验收契约/证据元数据 + 迁移 checksum/并发锁 + 证据链 + 测试网真实执行闸门 + BUY 复审 + health↔gate 一致性) / V11.8 Docker 生产运行时 + 主网就绪自检(SQLite WAL/busy_timeout/foreign_keys + MAINNET_READINESS_CHECK + 多阶段 Dockerfile + compose 持久化卷 + CI docker smoke) / V12 小资金主网接管(风控参数接线 + HODL 基准 §24-25 + 主网只读接管 §10-11 + 局域网 Web §5 + 每日复盘扩展 §37 + SQLite 备份 §31 + 启动前清单 §32)。

## at01_common(基础设施)

| 文件 | 内容 |
|------|------|
| `settings.py` | 全部配置项(V1~V7,风控百分比/评分阈值/regime/AI) |
| `timeframe.py` | V7 统一时间粒度(interval→秒/bar数/年化因子, 全系统唯一来源) |
| `database.py` | 惰性引擎 + AsyncSessionLocal 代理 + reset_engine(测试)+ `SCHEMA_VERSION` 标记(V11.2 P1-5); V11.8 P0-3 SQLite 生产 pragma(WAL/busy_timeout/foreign_keys, connect 事件 + `check_same_thread=False`) |
| `logger.py` | structlog 配置 + LoggerMixin |
| `models.py` | 26 张 ORM 表(含 V10 `KillSwitchState` 急停单行表、V10.7 `ExecutionEvent` 事件日志表、V12 §24 `HodlBenchmarkState` 单行基线表) |
| `runtime_supervisor.py` | V11.5 P0-2 RuntimeSupervisor: 统一 spawn 命名后台任务 + 运行/完成/取消/异常跟踪, critical 崩溃 → 安全态 + 急停, graceful shutdown 幂等取消回收 |
| `runtime_health.py` | V11.5 P1-1 运行时健康快照: `build_runtime_health` + 七态分类器(KILLED/RECOVERY/PAUSED/REDUCE_ONLY/DEGRADED/TRADING/SAFE), `/api/metrics` 聚合为单一 `health` 字段 |
| `schema_check.py` | V11.5 P0-4 数据库 schema 检查(全列 inventory, 捕获 create_all 静默列漂移; 配 `test_v153_schema_check.py`) |
| `migrations.py` | V11.6 P1-4 最小前向迁移框架: `upgrade_schema`(前向 DDL 幂等应用 + 方言感知)+ `detect_dialect`/`list_migrations`/`_split_statements`; 配 `migrations/*.sql` + `schema_version` 簿记表; V11.7 P1-1 `checksum`(SHA-256, 同版本异内容 FAIL FAST)+ P1-2 迁移并发锁(asyncio.Lock + version PK 兜底) |
| `soak.py` | V11.6 P1-7/P1-8 测试网 soak 运行器 + 运行时证据记录; V11.7 P0-2 优雅停机阶梯(POST /api/shutdown → terminate → kill)+ P0-3 验收契约 `evaluate_soak_result` + P0-4 可复现元数据(run_id/git_sha/duration/final_state/acceptance_result, 目录 `logs/soak/<run_id>/`) |
| `evidence_chain.py` | V11.7 P1-3 测试网证据链: `build_evidence_chain`(run_id→order→fill→position→lot→sell_allocation→exchange_truth→reconciliation→soak_result)+ `chain_consistency_issues`(orphan_fill/fill_mismatch/buy_lot_mismatch/…)+ `load_run_evidence` |
| `testnet_gate.py` | V11.7 P1-4 测试网真实执行闸门: 真实(非纸面)执行须 `BINANCE_TESTNET=true`+`PAPER_TRADING=false`+`RUN_TESTNET_TRADING=1`+`live_trading=false`+测试网 key 齐备, 否则 BLOCKED; **绝对禁止主网误执行** |
| `mainnet_readiness.py` | V11.8 P0-4 主网就绪自检: `mainnet_readiness_check`(九项确定性判定: 连主网/非纸面/显式确认/API 权限确认/单币/配置审计/非急停/git_sha+主网端点)+ `format_readiness_report`; 主网启动前强制, 任一不满足 BLOCKED |
| `bootstrap.py` | V11.6 P2 `inject_sys_path`: 把 atXX 分层目录注入 sys.path(幂等, 从 run.py 顶部内联循环抽出) |
| `wiring.py` | V11.6 P2 `wire_system(system)`: 承接原 initialize() 全部引擎装配逻辑(日志/审计/建库/守卫/风控/执行/分析/行情/对账/生命周期) |
| `runtime.py` | V11.6 P2 `run(system_cls)`: 承接原 main() 信号驱动 initialize/start/stop 生命周期编排 |

> V11.2 P1-6: 已删除死模块 `at01_common/time.py`(三函数全仓库无引用)。

## at90_web(监控面板)

| 文件 | 内容 |
|------|------|
| `web_app.py` | FastAPI 装配 + start_server |
| `web_api_routes.py` | 全部 REST 路由(14 端点, 含 V10 急停/恢复) |
| `web_ws_stream.py` | /ws 推送(2s) + broadcast 成交事件 |
| `web_state.py` | SystemState 引擎句柄容器 |
| `web_serve_standalone.py` | 前端独立启动(不跑交易引擎) |
| `static/index.html` | 单页面板(行情/分析/环境/风控/持仓/订单/信号) |

## at10_market(行情)

| 文件 | 内容 |
|------|------|
| `market_engine.py` | WS 消息路由(aggTrade/raw/kline/depth/ticker)/状态预热/批量落库/EventBus 发布 |
| `market_models.py` | TradeTick / KlineBar / DepthState / SymbolState |
| `market_rest_client.py` | 签名/时间同步/下单撤单查询 |
| `market_ws_client.py` | 组合流/自动重连/动态订阅 |

## at20_analytics(分析)

| 文件 | 内容 |
|------|------|
| `engine.py` | AnalyticsEngine + MarketAnalytics 快照(全指标) |
| `indicators.py` | VWAP(σ通道) / Delta / CVD(斜率) |
| `whale.py` | 大单检测(静态阈值+P99 动态) |
| `accumulation.py` | 吸筹(横盘+净流入+大单买方+买压增强,4 规则) |
| `regime.py` | MarketRegimeEngine(BULL/NORMAL/SIDEWAY/VOLATILE/BEAR/PANIC 六态) + 策略调整建议 |
| `alpha.py` | AlphaEngine 综合评分(5 因子+SOL/BTC 相对强弱) |
| `bus.py` | EventBus(Redis Stream, 发布/消费组; V10.6 ACK=业务成功 + DLQ + recover_pending; V10.7 事件信封 event_id/event_time/event_version/source + 有界内存去重) |

## at70_journal(日志/复盘)

| 文件 | 内容 |
|------|------|
| `daily_report.py` | V9.0 每日复盘(聚合决策/成交/绩效 → `reports/YYYY-MM-DD.md`); V12 §37 新增账户与持仓(SOL/现金/敞口/未实现/回撤)+ HODL 对标(Alpha/PnL)+ 交易活动(买卖笔数/手续费)+ 交易门/对账健康 |
| `hodl_benchmark.py` | V12 §24-25 HODL 基准: 接管时刻冻结「初始权益/初始 SOL 数量/初始 SOL 价格」单行基线, `compute_benchmark` 纯函数算 Adaptive/HODL/Cash 权益 + Alpha |
| `trading_journal.py` | V9.0 交易日志(closed trade 落库) |

## at30_strategy(策略)

| 文件 | 内容 |
|------|------|
| `strategy_engine.py` | 策略调度 + 融合决策调用 + 信号落库 + AI 顾问循环 |
| `strategy_base.py` | BaseStrategy / **Signal 标准格式**(reason 列表+score+indicators) |
| `strategy_buy.py` | Entry 评分模型(5 维加权) |
| `strategy_sell.py` | Exit(分批止盈/移动止盈/趋势退出 + exit_tags 标签) |
| `strategy_grid.py` | 网格(±2%×10 格, 越界重设) |
| `strategy_trend.py` | EMA 金叉死叉 + CVD 确认 |
| `strategy_decision.py` | **DecisionEngine**(加权投票+regime 系数+冲突解消+绩效调权+未知权重拒绝) |
| `strategy_signal_tracker.py` | 信号结果跟踪(signal_result 表) |
| `strategy_identity.py` | V6 StrategyType 枚举+能力声明(唯一命名源) |
| `strategy_journal.py` | V4 决策日志(decision_log 表) |
| `strategy_ai_advisor.py` | AI 参数顾问(供应商 openai/qwen/deepseek, 不交易) |
| `llm_config.py` | V9 AI 供应商配置中心(Key 从 .env 读, `resolve_provider` 供应商选择: openai/qwen/deepseek) |

## at60_execution(执行)

| 文件 | 内容 |
|------|------|
| `execution_executor.py` | 执行主流程: 幂等→状态机闸门→落库→下单→记账→绩效; V10 急停撤单; V10.6 强一致记账 + symbol 锁 + fill_idempotency_key + 数量分离 + exchangeInfo 禁 BUY; V10.7 恢复原语 apply_recovered_fill / rebuild_buy_accounting; V11.1 P0-4 rebuild_sell_accounting(SELL 从 DB lot 重放 + 内存重同步) |
| `execution_paper_broker.py` | 纸面交易(滑点/手续费/现金管理) |
| `execution_state.py` | OrderState/TradeState 状态机 + TradeStateMachine |
| `reconciliation.py` | V8 持仓对账 + V10 `reconcile_account` 权益对账(超容差返回漂移) |
| `startup_reconciler.py` | V10 启动崩溃窗口恢复(确定性自愈 + 歧义检测) |
| `mainnet_takeover.py` | V12 §10-11 主网只读接管: 首次主网启动快照(余额/SOL/挂单/成交历史)+ 对账(意外挂单/持仓漂移)→ 记 HODL 基线; `allowed=false` → 急停冻结 |
| `cross_reconciler.py` | V10.4 三维交叉对账(Order/Fill/Ledger/Lot 逐笔核对, 漂移→急停; ledger 仅纸面核对, 实盘跳过) |
| `exchange_filters.py` | V10.5 交易规则过滤(stepSize/tickSize/minQty/minNotional 对齐) |
| `execution_events.py` | V10.7 订单执行事件日志(append-only 审计, event_id 非空唯一) |
| `order_recovery.py` | V10.7 订单恢复引擎(UNKNOWN/SUBMITTING 周期收敛 + RECOVERY_REQUIRED 账务重建); V11.1 P0-4 SELL 走 rebuild_sell_accounting 自愈(移除人工冻结) |
| `exchange_truth_reconciler.py` | V10.7 交易所真相对账(订单/成交维度, fill_truth/orphan_trade 检测); V11.1 P0-1 分页完整性(truth_incomplete/pagination_exhausted/trade_duplicate/trade_id_gap)+ 窗口从订单时间推导 + 不完整降级不冻结 |
| `fee_calculator.py` | V11.1 P0-2 统一手续费计价: `FeeCalculator`(USDT=quote / SOL=base 折算; 其它资产 → `unpriced` 降级不静默 fee=0)+ `FillFee`/`FeeResult`(ZERO/PRICED/UNPRICED) |
| `ledger_reconstruction.py` | V11.1 P0-3 账本重建引擎: 交易所真相 → Trades → Orders → Buy Lots → Sell Allocations → Position → Cash → Ledger → Equity; 幂等 + dry-run/apply(单事务)+ 守恒检查 + SAFE_MODE |
| `reconciliation_matrix.py` | V11.1 P0-5 对账矩阵: 统一四态判定(PASS/DEGRADED/RECOVERY_REQUIRED/KILLED)+ 单一对账器不得 kill(跨源单源 kill / 内部一致性需 ≥2 对账器佐证) |
| `observability.py` | V11.1 P1-4 生产可观测性: `MetricsStore`(计数器/仪表/延迟样本/策略归因)+ `evaluate_alerts` 阈值告警 + `order_failure_rate`/`percentile_rank` + `strategy_attribution` |
| `drift.py` | V11.2 P0-3 资金漂移计算: 三向 equity/position/cash 明确 numerator/denominator/零基/单边失配 1.0; `truth_incomplete`/missing → `trusted=False`(绝不 0 drift); `compute_drift` 纯函数 |

## at50_risk(风控)

| 文件 | 内容 |
|------|------|
| `risk_manager.py` | 审批链 + 百分比风控 + 异常保护(价格/静默/连续失败) |
| `risk_position.py` | 持仓账务 + 可买可卖额度 + 快照落库 |
| `risk_portfolio.py` | **PortfolioEngine**(成本管理: 降本/保本价/成本曲线/再平衡) |
| `risk_drawdown.py` | 回撤控制 |
| `risk_breaker.py` | 熔断(回撤/日亏, 冷却期) |
| `risk_allocation.py` | V4 PortfolioAllocator(动态敞口+双仓分配+risk_adjustment_factor) |
| `risk_buckets.py` | V4 BucketPositionManager(核心/交易双仓, 跨仓拒绝) |
| `risk_tiered.py` | V4 分级回撤五档(10~50%, 逐级收紧) |
| `risk_sizing.py` | V4/V5 PositionSizer(评分定仓+dynamic_trade_limit) |
| `risk_ledger.py` | V6 PortfolioLedger(双仓独立成本+reconcile 对账) |
| `risk_killswitch.py` | V10 急停开关(持久化单行, 不自动复位, arm/disarm/persist/load) |
| `risk_account_ledger.py` | V9 M3 AccountLedgerWriter(逐笔落 USDT/SOL 两行审计流水) |
| `risk_lot.py` | V10.3 LotTracker(FIFO 批次会计 + SellAllocation 分配) |
| `risk_state.py` | V10.5/V10.6/V10.7 风险状态机(NORMAL/REDUCE_ONLY/PAUSED/KILLED/RECOVERY_CHECK 五态 + can_buy/can_sell 方向闸门; KILLED→RECOVERY_CHECK→NORMAL 两步解禁) |
| `system_lifecycle.py` | V11.1 P1-3 顶层生命周期状态机(INIT/WARMING_UP/SYNCING/SELF_CHECK/READY/TRADING/DEGRADED/RECOVERY/SAFE_MODE/STOPPED + `trading_gate`/`reduce_gate` 四维 CanTrade 闸门) |
| `fund_circuit_breaker.py` | V11.1 P1-5 资金级熔断: Equity/Position/Cash 三向漂移分级(0.1%/0.2%/0.5%)→ NONE/REDUCE_ONLY/PAUSE/KILL; Position·Cash 首选 REDUCE_ONLY, Equity 逐级收紧到 KILL; `classify_drift` 纯函数 + `FundCircuitBreaker.assess` 三向聚合取最严重 |
| `trading_gate.py` | V11.2 P0-1/P0-2 统一交易闸门(单一权威): 组合生命周期+风险态+行情健康+交易所健康+对账+资金熔断六维; `can_open_position`/`can_reduce_position`/`can_cancel_order` 三接口 + `snapshot` 审计 |

## at80_backtest(回测)

| 文件 | 内容 |
|------|------|
| `backtest_portfolio.py` | **V7 主力**: 真实策略管线回测(StrategyEngine 驱动+双仓+对账+滑点敏感性) |
| `backtest_execution.py` | V7 执行模型: SlippageModel / NextBarExecutor(次bar) / AsOfJoiner(BTC asof) |
| `backtest_engine.py` | 早期简单回测(评分近似) |
| `backtest_run.py` | CLI |
| `backtest_walkforward.py` | Walk-Forward(训练/验证滚动窗口+过拟合间隙) |
| `backtest_robustness.py` | V11.1 P1-1 Backtest V2: 四维鲁棒性矩阵(时间×市场态×参数扰动×执行成本)+ `compute_robustness` 鲁棒性评分 + `RobustnessMatrixRunner`(注入 run_cell)+ `classify_regime` 市场态分类 + 公共 `robustness_score` |
| `backtest_optimizer.py` | V11.1 P1-2 Optimizer V2: 网格搜索→Walk-Forward→鲁棒性→风险调整排序(`build_grid` / `OptimizerV2` / `build_param_result` / `rank_params` / `compute_overfit_gap`), 防过拟合 |

## run.py(主编排器)

装配顺序: init_db → RiskManager(加载持仓) → PortfolioEngine → AlphaEngine →
SignalTracker(加载未完成) → StrategyEngine → AnalyticsEngine → MarketEngine(启动) →
RegimeEngine → 注册 Web 状态 → 后台任务(risk/regime/snapshot/tracker/ai/web)。

## tests/(1236 个)

| 文件 | 覆盖 |
|------|------|
| `unit/test_indicators.py` | VWAP/Delta/CVD |
| `unit/test_whale_accumulation.py` | 大单/吸筹 |
| `unit/test_risk.py` | 风控(V2 百分比+异常保护) |
| `unit/test_strategies.py` | 四策略(V2 评分/止盈阶梯) |
| `unit/test_paper_broker.py` | 纸面交易+执行链路 |
| `unit/test_v2_modules.py` | Regime/总线/幂等/信号格式 |
| `unit/test_v3_modules.py` | Decision/Portfolio/Alpha/状态机/Exit标签/Tracker |
| `unit/test_v6_correctness.py` | 金融正确性: 账本对账/双仓成本/Invariant/四场景回归 |
| `unit/test_v7_no_lookahead.py` | 次bar执行/asof/bucket边界/equity恒等式/未知策略拒绝 |
| `integration/test_ws_routing.py` | WS 消息路由(真实币安格式) |
| `integration/test_web_api.py` | Web API(TestClient) |
| `unit/test_v10_killswitch.py` | V10 急停开关 + RiskManager 集成 |
| `unit/test_v10_reconciliation.py` | V10 权益对账 + 启动崩溃窗口恢复 |
| `integration/test_v10_emergency_api.py` | V10 急停/恢复 REST 端点 |
| `unit/test_v101_order_fill_ledger.py` | V10.1 Order→Fill→Ledger 链(状态迁移/异常分类/幂等摄入/手续费) |
| `unit/test_v102_reconciliation_execution.py` | V10.2 对账盲区(SUBMITTING/ExecutionAttempt/EXCHANGE_ONLY) |
| `unit/test_v103_lot_accounting.py` | V10.3 FIFO Lot 会计(已实现盈亏/分配/对账不变量) |
| `unit/test_v104_cross_reconcile.py` | V10.4 三维交叉对账(Order/Fill/Ledger/Lot) |
| `unit/test_v105_eventbus_dlq.py` | V10.5/V10.6 事件总线死信队列 + 有限重试 + ACK 语义 |
| `unit/test_v105_exchange_filters.py` | V10.5 交易规则过滤(stepSize/tickSize/minQty) |
| `unit/test_v105_ws_gap_recovery.py` | V10.5 WS 断线回补(幂等合并) |
| `unit/test_v105_reduce_only.py` | V10.5 REDUCE_ONLY(卖出不得超持仓) |
| `unit/test_v105_risk_state.py` | V10.5 风险状态机(NORMAL/PAUSED/KILLED) |
| `unit/test_v106_accounting_tx.py` | V10.6 成交后强一致记账 + RECOVERY_REQUIRED + 急停冻结 |
| `unit/test_v106_accounting_lock.py` | V10.6 symbol 级记账互斥锁 |
| `unit/test_v106_fill_idempotency.py` | V10.6 OrderFill 幂等键 fill_idempotency_key |
| `unit/test_v106_signal_exec_qty.py` | V10.6 Signal 与 Execution 数量分离 |
| `unit/test_v106_exchange_info_block.py` | V10.6 ExchangeInfo 失败禁 BUY |
| `unit/test_v106_risk_reduce_only.py` | V10.6 风险状态机 REDUCE_ONLY + can_buy/can_sell |
| `unit/test_v107_execution_events.py` | V10.7 订单执行事件日志(append-only + 幂等) |
| `unit/test_v107_event_envelope.py` | V10.7 事件信封 + 事件幂等 |
| `unit/test_v107_order_recovery.py` | V10.7 订单恢复引擎(UNKNOWN 收敛 / RECOVERY_REQUIRED 重建 / 幂等) |
| `unit/test_v107_exchange_truth.py` | V10.7 交易所真相对账(fill_truth / orphan_trade / 窗口过滤) |
| `unit/test_v107_recovery_check.py` | V10.7 风险状态机 RECOVERY_CHECK(两步解禁) |
| `unit/test_v107_invariants.py` | V10.7 10 个核心不变量测试 |
| `unit/test_v107_chaos.py` | V10.7 Chaos 测试(超时/重复成交/部分成交/DB回滚/未知订单) |
| `unit/test_v110_market_consistency.py` | V11.0 行情一致性(F13 @aggTrade 统一 + F10 myTrades 分页); V11.1 P0-1 分页完整性(耗尽/重复/跳号) |
| `unit/test_v111_exchange_truth_v2.py` | V11.1 P0-1 Exchange Truth V2(不完整降级 / 重复去重 / 跳号不影响核对 / 窗口从订单时间推导) |
| `unit/test_v112_fee_accounting.py` | V11.1 P0-2 手续费计价(计价器 4 态 / 汇总 unpriced / 成交指标标记 / 落库 status / 摄入降级 pause) |
| `unit/test_v113_ledger_reconstruction.py` | V11.1 P0-3 账本重建(持仓/lot/分配重建 + FIFO 盈亏 + 现金/账本守恒 + SAFE_MODE + 幂等 + dry-run/apply) |
| `unit/test_v114_sell_recovery.py` | V11.1 P0-4 SELL 账务重建(DB lot FIFO 重放 + 平均成本盈亏 + 清仓归零 + 幂等 + 分歧内存重同步 + 交易所补齐 + 真实手续费 + 超卖截断) |
| `unit/test_v115_reconciliation_matrix.py` | V11.1 P0-5 对账矩阵(档位映射 + 空/可观测性/降级/需恢复/跨源单源 kill/单一内部不 kill/双对账器佐证 kill/KILLED 优先) |
| `unit/test_v116_backtest_robustness.py` | V11.1 P1-1 Backtest V2(矩阵枚举 750 格 / 市场态分类 / 鲁棒性评分 / 编排器容错) |
| `unit/test_v117_optimizer_v2.py` | V11.1 P1-2 Optimizer V2(网格展开 / 过拟合间隙 / 夏普 / 风险调整得分 / 派生字段 / 排序防过拟合 / 编排容错) |
| `unit/test_v118_system_lifecycle.py` | V11.1 P1-3 System Lifecycle(线性启动链 / 非法迁移 / 降级恢复 / SAFE_MODE / STOPPED / trading_gate·reduce_gate 四维闸门) |
| `unit/test_v119_observability.py` | V11.1 P1-4 生产可观测性(订单失败率 / 百分位 / MetricsStore 采集 / 五类阈值告警 + 聚合 + 自定义阈值 / 策略归因) |
| `unit/test_v120_circuit_breaker.py` | V11.1 P1-5 资金级熔断(drift_pct / classify_drift 三档分级边界 / Equity 逐级收紧 / Position·Cash 首选 REDUCE_ONLY / 三向聚合取最严重 / 决策序列化) |
| `unit/test_v121_trading_gate.py` | V11.2 P0-1/P0-2 统一交易闸门(六维组合 / 三接口 / 降级恢复期禁买可减 / SAFE_MODE 数据可信可减 / KILLED 全禁 / 撤单除 STOPPED 放行 / 快照) |
| `unit/test_v122_drift.py` | V11.2 P0-3 资金漂移定义(对称/权益比例 / 零基 / 单边失配 1.0 / missing·truth_incomplete 不可信不 0 drift) |
| `unit/test_v123_fund_breaker_chain.py` | V11.2 P0-4 资金熔断执行链(真相→漂移→assess→决策端到端: 一致账本 NONE / 权益逐级收紧 / 持仓·现金 REDUCE_ONLY·PAUSE / truth_incomplete·missing·零权益绝不误判 KILL) |
| `unit/test_v124_fault_injection.py` | V11.2 P0-5 端到端故障注入(真实闸门栈装配 24 场景: 最终态 PASS/DEGRADED/REDUCE_ONLY/RECOVERY/KILLED + 不变量「故障绝不继续 BUY」+ KILLED 需确认恢复 / truth_incomplete 禁开) |
| `unit/test_v125_financial_invariants.py` | V11.2 P0-6 财务不变量最终审计(Base/Lots/SellAllocation/Cash/Equity 五守恒均允许手续费 + 守恒破坏 → 对账矩阵 → 禁开仓) |
| `unit/test_v126_lifecycle_runtime.py` | V11.2 P1-1/P1-2 真实运行生命周期 + 可观测性接入(迁移审计轨迹 + apply_reconcile_verdict 判定→生命周期 + 端到端降级禁开/恢复可开 + record_execution/reconcile_verdict/breaker_action 采集 + ws_silence_seconds) |
| `unit/test_v127_backtest_acceptance.py` | V11.2 P1-3 Backtest 最终验收(真实策略管线多市场态 + 单边熊市: 全量 bar / 对账 balanced / 曲线合法 / 基准·风险指标有限 / 胜率·盈利因子自洽) |
| `unit/test_v128_config_audit.py` | V11.2 P1-4 生产配置审计(Settings.validate: 实盘缺 key / 空标的 / 三桶比例和≠1 拦截 + 默认纸面·测试网通过) |
| `unit/test_v129_schema_audit.py` | V11.2 P1-5 数据库迁移审计(schema 稳定性锚点: 25 表清单 + 资金守恒关键列 + SCHEMA_VERSION + create_all 幂等) |
| `unit/test_v130_symbol_freeze.py` | V11.3 P0-2 冻结单币 SOLUSDT(默认 SOLUSDT + 非 SOLUSDT fail-fast) |
| `unit/test_v133_settings_failfast.py` | V11.3 P0-3 Settings 全量 Fail-Fast 审计 |
| `unit/test_v134_trading_gate_audit.py` | V11.3 P0-4 TradingGate 最终审计(入口全覆盖 + 防绕过) |
| `unit/test_v135_recovery_restart.py` | V11.3 P0-5 Recovery 状态机压力审计(重启语义) |
| `unit/test_v136_task_lifecycle.py` | V11.3 P0-7 Memory/Task Leak 审计(create_task 生命周期) |
| `unit/test_v137_db_consistency.py` | V11.3 P0-8 数据库一致性审计(运行时 schema / 唯一约束 / 单行表) |
| `unit/test_v138_fee_accounting_final.py` | V11.3 P0-9 手续费会计最终审计 |
| `unit/test_v139_observability_hardening.py` | V11.3 P0-10 可观测性加固(样本有界 / 恢复计数 / 告警可达) |
| `unit/test_v140_mainnet_guard.py` | V11.3 P1-4 默认禁主网守卫 |
| `unit/test_v141_optimizer_proposal_only.py` | V11.3 P1-7 Optimizer 只产 proposal 守约 |
| `unit/test_v130_run_supervisor.py` | V11.4 P0-8 run.py 运行时监督审计(局部导入 NameError 死路径) |
| `unit/test_v142_abnormal_never_buy.py` | V11.4 P0-3 异常态绝不继续 BUY(真实风控 + 统一闸门 + 静态防绕过) |
| `unit/test_v143_recovery_state_machine_audit.py` | V11.4 P0-4 恢复状态机穷举审计(全迁移矩阵 + 方向闸门) |
| `unit/test_v146_runtime_report.py` | V11.4 P1-4 运行报告机制(每日复盘附「运行状态」快照) |
| `unit/test_v147_observability_final.py` | V11.4 P1-5 Observability 最终检查(消除死指标) |
| `long_running/test_24h_soak.py` | V11.4 P0-2 24h 长跑仿真(24 压缩周期 + 故障注入财务不变量) |
| `long_running/test_72h_soak.py` | V11.4 P0-2 72h 长跑仿真(72 压缩周期 + DB/手续费/漂移故障注入) |
| `long_running/test_crash_restart_soak.py` | V11.4 P0-5 崩溃/重启浸泡(crash/restart soak) |
| `long_running/test_reconciliation_soak.py` | V11.4 P0-2 对账长跑仿真(对账不一致/漂移分级/对账后恢复交易) |
| `long_running/test_recovery_soak.py` | V11.4 P0-2 恢复/重启长跑仿真(kill switch/recovery/restart/recovery-then-trade) |
| `smoke/test_testnet_smoke.py` | V11.4 P0-6 真实币安测试网只读冒烟(不下单, -m testnet 隔离) |
| `integration/test_v150_web_security.py` | V11.5 P0-1 Web 写接口鉴权(`X-Admin-Token` 空锁死/错 401)+ `API_HOST` 出厂回环 + `0.0.0.0` 需令牌 fail-fast |
| `unit/test_v151_runtime_supervisor.py` | V11.5 P0-2 RuntimeSupervisor(spawn 命名任务 / critical 崩溃安全态 + 急停 / graceful shutdown 幂等取消) |
| `integration/test_v151_runtime_supervisor_system.py` | V11.5 P0-2 RuntimeSupervisor 系统集成(装配进 run 链路 / 停机回收) |
| `testnet/test_v152_testnet_order_lifecycle.py` | V11.5 P0-3 真实测试网下单全生命周期(下单→成交→账本→对账; opt-in `RUN_TESTNET_TRADING=1`, CI 排除) |
| `unit/test_v153_schema_check.py` | V11.5 P0-4 数据库迁移 schema 检查(schema 稳定性锚点 + 全列 inventory, 捕获 create_all 静默列漂移) |
| `unit/test_v154_runtime_health.py` | V11.5 P1-1 运行时健康快照(七态分类器 + `/api/metrics` health 聚合; 14 条) |
| `unit/test_v155_fault_injection.py` | V11.5 P1-2 运行时故障注入(critical 崩溃 SAFE_MODE 禁 BUY / 停机在途信号丢弃 / WS 断连禁开→重连收敛; 4 条) |
| `unit/test_v160_buy_safety_contract.py` | V11.6 P0-2 BUY 安全契约(停机/关键任务两维收口到统一闸门, 防停机窗口在途信号偷建仓) |
| `unit/test_v161_live_financial_truth.py` | V11.6 P0-3 实盘财务真相审计(AccountLedger 仅纸面落库, 实盘锚定交易所对账链) |
| `unit/test_v162_runtime_health_contract.py` | V11.6 P1-2 运行时健康契约(health.can_buy/can_sell 直接取自统一闸门) |
| `unit/test_v163_runtime_supervisor_audit2.py` | V11.6 P1-3 RuntimeSupervisor 第二轮审计(回调异常/取消/无重启契约) |
| `unit/test_v164_db_migration.py` | V11.6 P1-4 数据库迁移框架(detect_dialect/list_migrations/split/upgrade 幂等/基线/init_db 无漂移) |
| `unit/test_v165_soak.py` | V11.6 P1-7/P1-8 soak 运行时证据纯逻辑(证据行提取/迁移检测/摘要; 11 条) |
| `unit/test_v166_run_slim.py` | V11.6 P2 run.py 瘦身回归(注入幂等 + 交易语义方法完整在位 + wire_system/run 协程可调用; 6 条) |
| `unit/test_v167_soak_shutdown.py` | V11.7 P0-2 soak 优雅停机(shutdown→terminate→kill 阶梯, 失败/中断安全; 12 条) |
| `unit/test_v168_soak_acceptance.py` | V11.7 P0-3 soak 验收契约(evaluate_soak_result PASS/FAIL/BLOCKED, can_buy 曾 false 非失败; 13 条) |
| `unit/test_v169_db_migration_checksum.py` | V11.7 P1-1 迁移 checksum(SHA-256, 同版本异内容 FAIL FAST; 6 条) |
| `unit/test_v170_soak_metadata.py` | V11.7 P0-4 可复现元数据(run_id/git_sha/duration/acceptance_result; 9 条) |
| `unit/test_v171_migration_concurrency.py` | V11.7 P1-2 迁移并发安全(asyncio.Lock 惰性取锁 + version PK 兜底; 2 条) |
| `unit/test_v172_evidence_chain.py` | V11.7 P1-3 测试网证据链(build_evidence_chain + chain_consistency_issues + load_run_evidence; 16 条) |
| `unit/test_v173_testnet_gate.py` | V11.7 P1-4 测试网真实执行闸门(BLOCKED 条件 + preflight 报告; 10 条) |
| `unit/test_v174_runtime_health_evidence_consistency.py` | V11.7 P1-6 health↔gate 一致性(can_buy/can_sell 九维阻断逐字一致; 12 条) |
| `unit/test_v175_mainnet_readiness.py` | V11.8 P0-4 主网就绪自检(九项阻断/原因累积/报告格式/live_confirm 大小写空白不敏感/全绿 allowed; 13 条) |
| `unit/test_v176_sqlite_pragmas.py` | V11.8 P0-3 SQLite pragma(非 SQLite 跳过/施加三 pragma/真实连接 journal_mode=wal+busy_timeout+foreign_keys; 4 条) |
