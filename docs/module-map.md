# 模块清单(代码地图)

> 目录即包名,文件名带模块前缀。检索代码从这里出发。
> V7 状态: 217 测试 / 回测=实盘同一策略代码 / 对账恒平衡。

## at01_common(基础设施)

| 文件 | 内容 |
|------|------|
| `settings.py` | 全部配置项(V1~V7,风控百分比/评分阈值/regime/AI) |
| `timeframe.py` | V7 统一时间粒度(interval→秒/bar数/年化因子, 全系统唯一来源) |
| `database.py` | 惰性引擎 + AsyncSessionLocal 代理 + reset_engine(测试) |
| `logger.py` | structlog 配置 + LoggerMixin |
| `models.py` | 12 张 ORM 表(+position_bucket/decision_log/ai_parameter_history) |
| `time.py` | 时间工具 |

## at10_web(监控面板)

| 文件 | 内容 |
|------|------|
| `web_app.py` | FastAPI 装配 + start_server |
| `web_api_routes.py` | 全部 REST 路由(12 端点) |
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
| `bus.py` | EventBus(Redis Stream, 发布/消费组) |

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
| `execution_executor.py` | 执行主流程: 幂等→状态机闸门→落库→下单→记账→绩效 |
| `execution_paper_broker.py` | 纸面交易(滑点/手续费/现金管理) |
| `execution_state.py` | OrderState/TradeState 状态机 + TradeStateMachine |

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

## at70_backtest(回测)

| 文件 | 内容 |
|------|------|
| `backtest_portfolio.py` | **V7 主力**: 真实策略管线回测(StrategyEngine 驱动+双仓+对账+滑点敏感性) |
| `backtest_execution.py` | V7 执行模型: SlippageModel / NextBarExecutor(次bar) / AsOfJoiner(BTC asof) |
| `backtest_engine.py` | 早期简单回测(评分近似) |
| `backtest_run.py` | CLI |
| `backtest_walkforward.py` | Walk-Forward(训练/验证滚动窗口+过拟合间隙) |

## run.py(主编排器)

装配顺序: init_db → RiskManager(加载持仓) → PortfolioEngine → AlphaEngine →
SignalTracker(加载未完成) → StrategyEngine → AnalyticsEngine → MarketEngine(启动) →
RegimeEngine → 注册 Web 状态 → 后台任务(risk/regime/snapshot/tracker/ai/web)。

## tests/(217 个)

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
