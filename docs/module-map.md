# 模块清单(代码地图)

> 目录即包名,文件名带模块前缀。检索代码从这里出发。
> 当前状态: 679 测试 / 回测=实盘同一策略代码 / 对账恒平衡 / V11.0 深度审计 13 项资金正确性缺陷(F1-F13)全部修复(记账原子性 / 成交分页 / lot 幂等 / 成交流口径统一) / V11.1 P0-1 Exchange Truth V2(myTrades 分页完整性检测 + 降级不冻结) / V11.1 P0-2 Fee Accounting(统一 FeeCalculator, 不可计价手续费降级不静默 fee=0) / V11.1 P0-3 Ledger Reconstruction(交易所真相重建账务 + 守恒检查 + SAFE_MODE) / V11.1 P0-4 SELL Recovery(RECOVERY_REQUIRED SELL 从 DB lot 确定性重放, 消除人工冻结) / V11.1 P0-5 Reconciliation Matrix(统一四态判定 + 单一对账器不得 kill) / V11.1 P1-1 Backtest V2(四维鲁棒性矩阵 + 鲁棒性评分取代单一收益) / V11.1 P1-2 Optimizer V2(网格搜索→Walk-Forward→鲁棒性→风险调整排序, 防过拟合) / V11.1 P1-3 System Lifecycle(顶层状态机 + 四维 CanTrade 闸门) / V11.1 P1-4 生产可观测性(MetricsStore + 阈值告警 + 策略归因) / V11.1 P1-5 资金级 Circuit Breaker(Equity/Position/Cash 三向漂移分级 0.1%/0.2%/0.5%)。

## at01_common(基础设施)

| 文件 | 内容 |
|------|------|
| `settings.py` | 全部配置项(V1~V7,风控百分比/评分阈值/regime/AI) |
| `timeframe.py` | V7 统一时间粒度(interval→秒/bar数/年化因子, 全系统唯一来源) |
| `database.py` | 惰性引擎 + AsyncSessionLocal 代理 + reset_engine(测试) |
| `logger.py` | structlog 配置 + LoggerMixin |
| `models.py` | 25 张 ORM 表(含 V10 `KillSwitchState` 急停单行表、V10.7 `ExecutionEvent` 事件日志表) |
| `time.py` | 时间工具 |

## at10_web(监控面板)

| 文件 | 内容 |
|------|------|
| `web_app.py` | FastAPI 装配 + start_server |
| `web_api_routes.py` | 全部 REST 路由(14 端点, 含 V10 急停/恢复) |
| `web_ws_stream.py` | /ws 推送(2s) + broadcast 成交事件 |
| `web_state.py` | SystemState 引擎句柄容器 |
| `web_serve_standalone.py` | 前端独立启动(不跑交易引擎) |
| `static/index.html` | 单页面板(行情/分析/环境/风控/持仓/订单/信号) |

## at20_market(行情)

| 文件 | 内容 |
|------|------|
| `market_engine.py` | WS 消息路由(aggTrade/raw/kline/depth/ticker)/状态预热/批量落库/EventBus 发布 |
| `market_models.py` | TradeTick / KlineBar / DepthState / SymbolState |
| `market_rest_client.py` | 签名/时间同步/下单撤单查询 |
| `market_ws_client.py` | 组合流/自动重连/动态订阅 |

## at30_analytics(分析)

| 文件 | 内容 |
|------|------|
| `engine.py` | AnalyticsEngine + MarketAnalytics 快照(全指标) |
| `indicators.py` | VWAP(σ通道) / Delta / CVD(斜率) |
| `whale.py` | 大单检测(静态阈值+P99 动态) |
| `accumulation.py` | 吸筹(横盘+净流入+大单买方+买压增强,4 规则) |
| `regime.py` | MarketRegimeEngine(BULL/BEAR/PANIC/SIDEWAY) + 策略调整建议 |
| `alpha.py` | AlphaEngine 综合评分(5 因子+SOL/BTC 相对强弱) |
| `bus.py` | EventBus(Redis Stream, 发布/消费组; V10.6 ACK=业务成功 + DLQ + recover_pending; V10.7 事件信封 event_id/event_time/event_version/source + 有界内存去重) |

## at50_strategy(策略)

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

## at50_execution(执行)

| 文件 | 内容 |
|------|------|
| `execution_executor.py` | 执行主流程: 幂等→状态机闸门→落库→下单→记账→绩效; V10 急停撤单; V10.6 强一致记账 + symbol 锁 + fill_idempotency_key + 数量分离 + exchangeInfo 禁 BUY; V10.7 恢复原语 apply_recovered_fill / rebuild_buy_accounting; V11.1 P0-4 rebuild_sell_accounting(SELL 从 DB lot 重放 + 内存重同步) |
| `execution_paper_broker.py` | 纸面交易(滑点/手续费/现金管理) |
| `execution_state.py` | OrderState/TradeState 状态机 + TradeStateMachine |
| `reconciliation.py` | V8 持仓对账 + V10 `reconcile_account` 权益对账(超容差返回漂移) |
| `startup_reconciler.py` | V10 启动崩溃窗口恢复(确定性自愈 + 歧义检测) |
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

## at60_risk(风控)

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

## at70_backtest(回测)

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

## tests/(777 个)

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
