# 技术架构文档(V11.5)

> SOL/USDT 自动化量化交易系统 · Python 3.13 · asyncio 单进程异步架构

## 1. 系统架构总图

```
                         ┌─────────────────┐
                         │     Binance      │
                         │  WS + REST API   │
                         └────────┬────────┘
                                  │
        ┌─────────────────────────┼─────────────────────────┐
        │                         ▼                         │
        │              ┌─────────────────────┐              │
        │              │  at20_market        │              │
        │              │  Market Data Engine │              │
        │              │  · WS 组合流(自动重连)│              │
        │              │  · REST(签名/时间同步)│              │
        │              │  · 状态预热(K线/成交) │              │
        │              └────────┬────────────┘              │
        │                       │ tick                      │
        │                       ▼                           │
        │  ┌────────────────────────────────────────┐      │
        │  │  at30_analytics  Analytics Engine      │      │
        │  │  VWAP(σ通道) / Delta / CVD(斜率)       │      │
        │  │  Whale(P99动态) / 吸筹(4规则)          │      │
        │  │  EMA趋势 / OrderFlow / 量比 / 价格区间 │      │
        │  │  ────────────────────────────────      │      │
        │  │  Market Regime Engine (V2)            │      │
        │  │  BULL / NORMAL / SIDEWAY /           │      │
        │  │  VOLATILE / BEAR / PANIC             │      │
        │  │  ────────────────────────────────      │      │
        │  │  Alpha Engine (V3)                    │      │
        │  │  价格30+资金流25+趋势20+波动15+情绪10  │      │
        │  └────────────────┬───────────────────────┘      │
        │                   │ MarketAnalytics 快照          │
        │                   ▼                              │
        │  ┌────────────────────────────────────────┐      │
        │  │  at50_strategy  Strategy Engine        │      │
        │  │  Entry评分(5维加权,80买/60-80观察)      │      │
        │  │  Exit(分批止盈/移动止盈/趋势退出)       │      │
        │  │  Grid / Trend(EMA金叉死叉)             │      │
        │  │  ────────────────────────────────      │      │
        │  │  Decision Engine (V3)                 │      │
        │  │  多信号加权融合 -> 唯一 BUY/SELL/HOLD   │      │
        │  │  (策略权重×regime系数, 高分退出优先)    │      │
        │  └────────────────┬───────────────────────┘      │
        │                   │ 融合信号(score/reason/indicators)
        │                   ▼                              │
        │  ┌────────────────────────────────────────┐      │
        │  │  at60_risk  Risk Engine                │      │
        │  │  百分比风控: 仓位40%/单笔5%/日亏5%/回撤15% │  │
        │  │  异常保护: 价格波动3%/行情静默/连续失败   │      │
        │  │  ────────────────────────────────      │      │
        │  │  Portfolio Engine (V3)                │      │
        │  │  成本管理: 卖出降本/保本价/成本曲线      │      │
        │  └────────────────┬───────────────────────┘      │
        │                   │ RiskDecision(数量/价格)        │
        │                   ▼                              │
        │  ┌────────────────────────────────────────┐      │
        │  │  at50_execution  Execution Engine      │      │
        │  │  幂等控制(信号去重)                     │      │
        │  │  交易状态机(防重复建仓)                 │      │
        │  │  PaperBroker(纸面) / 实盘限价+轮询确认  │      │
        │  └────────────────┬───────────────────────┘      │
        │                   │ 成交                          │
        │                   ▼                              │
        │  ┌────────────────────────────────────────┐      │
        │  │  SQLite (业务库, 默认) + Redis(可选, 默认关闭) │      │
        │  └────────────────┬───────────────────────┘      │
        │                   │                              │
        │                   ▼                              │
        │  ┌────────────────────────────────────────┐      │
        │  │  at10_web  Web Dashboard               │      │
        │  │  REST API + WS 实时推送 + 监控面板      │      │
        │  │  (独立启动模式 web_serve_standalone)    │      │
        │  └────────────────────────────────────────┘      │
        │                                                 │
        │  ┌────────────────────────────────────────┐      │
        │  │  AI Advisor (at50_strategy)            │      │
        │  │  每日周期, 只输出参数建议, 不交易        │      │
        │  └────────────────────────────────────────┘      │
        │                                                 │
        │  ┌────────────────────────────────────────┐      │
        │  │  at70_backtest  回测                    │      │
        │  │  K线回放 / Walk-Forward(过拟合检测)     │      │
        │  └────────────────────────────────────────┘      │
        └─────────────────────────────────────────────────┘
```

## 2. 数据流程图

### 2.1 实时交易主链路(毫秒级)

```
Binance WS tick
  │
  ▼ (at20_market._handle_trade)
TradeTick ──┬──> 内存状态(trades deque / last_price)
            ├──> Redis pub-sub (market:trade:{symbol})  ← 可选, Redis 关闭时跳过
            ├──> Redis Stream (at:market:events)        ← EventBus(可选, 降级)
            ├──> 批量缓冲 -> SQLite trades 表(5s批量)
            └──> on_trade 回调
                   │
                   ▼ (run.py._on_trade)
        价格异常检测(risk.check_tick_anomaly, 单笔>3% -> 暂停)
        峰值价更新(positions.update_price)
                   │
                   ▼ (AnalyticsEngine.on_trade)
        VWAP/Delta/CVD/Whale/吸筹/EMA/OrderFlow/量比 更新
        -> MarketAnalytics 快照
                   │
                   ▼ (StrategyEngine.on_analytics)
        各策略 on_market -> list[Signal]
        DecisionEngine.decide(signals) -> Decision
            BUY/SELL: 融合信号落库 + 下发
            HOLD: 原始信号落库, 不下发
                   │
                   ▼ (run.py._on_signal)
        RiskManager.check -> RiskDecision(approved, qty, price)
            拒绝: 结束(观察档/熔断/异常/超限)
                   │
                   ▼ (ExecutionEngine.execute)
        幂等检查(策略:标的:方向 10s)
        状态机闸门(HOLDING 拒绝重复买入)
        订单落库(orders + signals.executing)
        PaperBroker/实盘下单 -> FILLED
        V4 评分定仓(Alpha×regime×回撤档×置信 -> 动态单笔限额)
        V5 卖出 bucket 闸门(下单前封顶交易仓, 核心仓不可被交易卖)
        双仓记账(BucketPositionManager -> position_bucket)
        交易状态机推进(HOLDING/EXIT_PENDING/CLOSED/IDLE)
        strategy_performance 更新
        on_fill -> 经 source_strategy 路由回源策略(V6) + Web broadcast
```

### 2.2 周期任务

| 任务 | 周期 | 职责 |
|------|------|------|
| risk-loop | 5s | 权益/回撤/日内亏损更新, 行情静默检测 |
| regime-loop | 30s | 市场环境评估(BULL/BEAR/...)注入分析快照 |
| snapshot-loop | 60s | 持仓快照 -> position_snapshot(收益曲线) |
| signal-tracker-loop | 60s | 信号未来收益 -> signal_result(1h窗口) |
| ai-loop | 每日 | AI 参数建议(不交易, 变化记录 ai_parameter_history) |
| market-persist | 5s | trades/klines 批量落库 |

### 2.3 signal_result 闭环(AI 学习数据)

```
融合信号执行 -> signal_id 注册 Tracker
      │
      ▼ 每60s
未来收益更新: future_profit / max_profit / max_drawdown
      │
      ▼ 窗口结束(1h)
final=True 定稿
      │
      ▼
strategy_stats_from_db() -> DecisionEngine.update_weights()
                            (胜率高的策略加权, 低的降权)
                            + AI Advisor 输入
```

## 3. 模块分层与包名

| 目录(=包名) | 层级 | 模块 |
|------|------|------|
| `at01_common` | 基础 | settings / database(惰性引擎) / logger / models(25表) / timeframe(统一时间粒度) / runtime_supervisor(后台任务监督) / runtime_health(运行时健康快照) |
| `at10_web` | 展示 | web_app / web_api_routes / web_ws_stream / web_state / web_serve_standalone / static |
| `at20_market` | 行情 | market_engine / market_models / market_rest_client / market_ws_client / data_validator / market_futures_client |
| `at30_analytics` | 分析 | engine / indicators / whale / accumulation / regime / alpha / regime_hmm / regime_hmm_train / sentiment / bus(EventBus) |
| `at40_journal` | 日志 | daily_report / trading_journal |
| `at50_strategy` | 策略 | strategy_engine / strategy_base(Signal+source_strategy) / strategy_buy(entry) / strategy_sell(exit) / strategy_grid / strategy_trend / strategy_decision(未知权重拒绝) / strategy_identity(枚举) / strategy_journal / strategy_signal_tracker / strategy_ai_advisor / strategy_version / strategy_group / ai_parameter_guard / llm_config |
| `at50_execution` | 执行 | execution_executor / execution_paper_broker / execution_state(状态机) / execution_events / fee_calculator / ledger_reconstruction / order_recovery / cross_reconciler / exchange_truth_reconciler / reconciliation / reconciliation_matrix / startup_reconciler / drift / exchange_filters / observability |
| `at55_portfolio` | 组合 | core_manager / portfolio_manager |
| `at60_risk` | 风控 | risk_manager / risk_position / risk_portfolio / risk_drawdown / risk_breaker / risk_allocation / risk_buckets / risk_tiered / risk_sizing / risk_ledger / risk_account_ledger / risk_lot / risk_state / risk_killswitch / system_lifecycle / trading_gate / fund_circuit_breaker |
| `at70_backtest` | 回测 | backtest_portfolio(真实策略管线+滑点+次bar) / backtest_execution(Slippage/NextBar/AsOf) / backtest_engine / backtest_run / backtest_walkforward / backtest_optimizer / backtest_robustness |
| `at80_optimizer` | 优化 | optimizer / report |
| `at90_deploy` | 部署 | Dockerfile / docker-compose / init.sql |

## 4. 数据库模型(25 张表)

| 表 | 用途 | 关键字段 |
|----|------|---------|
| klines | K线 | symbol+interval+open_time 唯一 |
| trades | 逐笔成交 | symbol+trade_id 唯一 |
| signals | 策略信号(V2+indicators) | score / reason / indicators JSON / status |
| orders | 订单 | client_order_id 唯一 / signal_id / strategy |
| order_intents | 下单意图(幂等去重) | intent_id 唯一 / order_id |
| order_fills | 逐笔成交明细 | order_id / price / quantity |
| execution_attempts | 执行尝试 | order_id / attempt / status |
| position_lots | FIFO 持仓批次 | client_order_id / 剩余数量+单位成本 |
| sell_allocations | FIFO 卖出分配 | lot_id / sell_client_order_id / quantity |
| positions | 持仓(账务) | avg_price / realized_pnl / peak_price |
| position_snapshot | 持仓快照(收益曲线) | equity / unrealized / realized(60s采样) |
| strategy_performance | 策略绩效 | win_rate / profit(strategy+symbol 唯一) |
| signal_result | 信号结果 | future_profit / max_profit / final |
| position_bucket | 双仓 | core/trade 独立数量+成本 |
| decision_log | 决策日志 | regime/置信/Alpha/双仓/现金 |
| ai_parameter_history | AI 参数历史 | 旧值/新值/原因/效果 |
| risk_events | 风控事件 | type / detail / timestamp |
| ai_advices | AI 建议 | 参数建议(不交易) |
| trade_records | 已平仓交易 | symbol / 开平仓价 / realized_pnl |
| strategy_versions | 策略版本 | strategy / version / 迁移审计 |
| trade_state | 交易状态(幂等) | symbol / 状态机态 |
| paper_state | 纸面状态 | symbol / 纸面持仓 |
| account_ledger | 现金/持仓流水(逐笔) | asset / change_amount / related_order_id |
| kill_switch_state | 急停开关(单行持久化 id=1) | armed / reason |
| execution_events | 执行事件审计 | event_type / order_id / timestamp |

## 5. 关键设计决策

| 决策 | 理由 |
|------|------|
| 单进程 asyncio(非微服务) | 交易链路毫秒级延迟要求, 避免跨进程序列化 |
| 目录=包名(atXX 前缀) | 号码即分层, import 路径自解释 |
| AI 只建议不交易 | 可控性: 规则负责实时决策, AI 负责慢速参数优化 |
| 纸面模式默认 | 安全: PAPER_TRADING=true 出厂默认 |
| 惰性 DB 引擎 | 测试环境隔离(reset_engine + sqlite 内存) |
| 状态机补齐迁移 | 纸面模式无挂单阶段, 成交时补 ENTRY_PENDING/EXIT_PENDING |
| EventBus 降级 | Redis 不可用时静默跳过, 主链路走内存回调 |
| 回测=实盘代码(V7) | 回测驱动真实 StrategyEngine, 禁止内嵌第二套策略 |
| 次bar执行(V7) | 信号 t 收盘 → t+1 开盘成交, 杜绝 look-ahead |
| 滑点敏感性(V7) | 执行模型显式 bps, 回测按 0/10/20bps 报告 |
| 未知配置拒绝(V7) | 无权重策略跳过而非静默默认值 |

## 6. V11.2 系统集成层(生命周期 / 闸门 / 可观测)

V11.2 不新增业务模块, 而是把 V11.1 的独立模块接进主链路, 形成「故障 → 闸门最终态」的系统级闭环。

```
对账循环(_reconcile_loop)                     风险循环(_risk_loop, 5s)
  ├─ exchange_truth findings                     ├─ 更新闸门健康信号(连接/行情健康)
  ├─ truth_complete 推导                          ├─ data_gap_seconds gauge
  ├─ compute_drift(equity/position/cash)         └─ evaluate_alerts(阈值告警 → 日志 + web)
  ├─ FundCircuitBreaker.assess → BreakerDecision
  │     └─ _apply_breaker_decision(REDUCE_ONLY/PAUSE/KILL→SAFE_MODE)
  ├─ ReconciliationMatrix.verdict → Severity
  │     └─ _apply_verdict → apply_reconcile_verdict(lifecycle, ...)
  └─ record_reconcile_verdict / record_breaker_action / reconcile_drift_pct
              │
              ▼
        TradingGate(六维) ── can_open_position / can_reduce_position / can_cancel_order
              │
              ▼
        _on_signal / _apply_core_action ── 计时 → record_execution → MetricsStore
```

**关键模块**:

| 模块 | 位置 | 职责 |
|------|------|------|
| `SystemLifecycle` | `at60_risk/system_lifecycle.py` | 顶层 10 态生命周期(INIT→…→TRADING / DEGRADED / RECOVERY / SAFE_MODE), 迁移审计轨迹 `history` |
| `apply_reconcile_verdict` | 同上 | 对账矩阵判定 → 生命周期迁移的纯函数映射 |
| `TradingGate` | `at60_risk/trading_gate.py` | 六维单一权威交易闸门(生命周期+风险态+行情+交易所+对账+熔断) |
| `FundCircuitBreaker` | `at60_risk/fund_circuit_breaker.py` | Equity/Position/Cash 三向漂移分级(REDUCE_ONLY/PAUSE/KILL) |
| `compute_drift` | `at50_execution/drift.py` | 三向漂移精确语义(missing-data 不 0 drift) |
| `MetricsStore` | `at50_execution/observability.py` | 计数器/仪表/延迟样本/策略 PnL + `evaluate_alerts` / `strategy_attribution` |
| `record_execution` 等 | 同上 | 执行/对账/熔断采集器纯函数 |
| `ReconciliationMatrix` | `at50_execution/reconciliation_matrix.py` | 统一各对账器差异处置(PASS/DEGRADED/RECOVERY_REQUIRED/KILLED) |
| `CrossReconciler` | `at50_execution/cross_reconciler.py` | Order/Fill/Ledger/Lot 四维交叉一致性 |

**启动 fail-fast**: `Settings.validate()` 在 `init_db()` 前拦截「实盘缺 key / 空标的 / 三桶比例和≠1」。
**schema 锚点**: `SCHEMA_VERSION`(V11.2)+ `test_v129_schema_audit.py` 钉死 25 表全列清单(259 列, 捕获 create_all 静默列漂移)。

**V11.3 生产加固**(不新增策略/币种/合约/高频/LLM 下单, 纯可靠性):
- 可观测性加固(`at50_execution/observability.py`): 延迟样本有界(`MAX_SAMPLE_LEN=10000`)、
  `recovery_streak`/`recoveries` 恢复计数接线(修复死指标)、告警降噪(仅状态切换时告警)。
- 停机前 `flush_events` 等待在途风险事件落库(`run.py`), 防审计事件丢失。
- 指标持久化评估: **保持内存 `MetricsStore`**, 不新增表 / 不引 Prometheus —— 见
  [`docs/metrics-persistence.md`](metrics-persistence.md)。

**V11.4 运行时验证**(无架构变更, 纯「长跑证明正确」加固):
- 长跑 soak + 故障注入财务不变量(`test_24h_soak.py`)、异常绝不继续 BUY、恢复状态机穷举审计、重启/崩溃 soak。
- run.py 运行时监督审计: 修复 6 处局部导入 NameError 死路径(`test_v130`)。
- 运行报告: 每日复盘附「运行状态」快照(`DailyReport._render_health` + `run._runtime_health`)。
- 可观测性补齐: 8 个「写而不读」计数器进 `snapshot()`、`data_gaps` 接线(`silence_active` 上升沿)。
- CI: ruff 正确性基线(E9+F)+ coverage 阈值 75%。

**V11.5 运维加固**(无架构变更, 纯「运行证明可靠」加固):
- Web 安全(P0-1): 写接口统一 `X-Admin-Token` 头鉴权; `API_HOST=127.0.0.1` 出厂默认; `0.0.0.0` 需非空 `WEB_ADMIN_TOKEN`。
- RuntimeSupervisor(P0-2): `at01_common/runtime_supervisor.py` 统一 spawn 命名后台任务, critical 任务崩溃 → 安全态 + 急停。
- 运行时健康快照(P1-1): `at01_common/runtime_health.py` `build_runtime_health` 七态分类器, `/api/metrics` 聚合为单一 `health` 字段。
- 测试网真实下单闭环(P0-3): `tests/testnet/test_v152_testnet_order_lifecycle.py`(opt-in `RUN_TESTNET_TRADING=1`)。
- 数据库迁移(P0-4): 无迁移框架记为已知缺口, schema 稳定性锚点 + 全列 inventory(`test_v153_schema_check.py`)兜底。
- 类型/静态审计(P1-3): mypy 适度接入(核心模块 only, 非 CI 门禁)+ ruff 移除 F401 全局忽略。
- 依赖/供应链(P1-4): `cryptography` / `websockets` 显式声明 + `uv.lock` 同步 + `pip-audit` 应用依赖 0 已知漏洞。

**V11.6 实盘财务真相与 AccountLedger 模式**(P0-3 审计 + P1-1 设计):
- 财务真相链逐表核对: Order/OrderFill/Position/PositionLot/SellAllocation 纸面与实盘均落库;
  `account_ledger`(审计账本, USDT+SOL 逐笔 before/change/after) **仅纸面(is_paper=True)落库**。
- 原因: 实盘现金余额来自交易所、仅在异步对账路径可得, 不在成交同步路径; `_apply_fill_accounting`
  的 `cash_before/cash_after` 在实盘恒为 `None`, `AccountLedgerWriter.record()` 被闸门跳过。
- **设计决策(保持不变)**: 实盘财务真相锚定交易所对账链, 不强行在实盘补写 AccountLedger ——
  同步镜像本地现金余额会引入「本地 vs 交易所」二次漂移源, 且无法可靠实现(现金维度已由
  `PositionReconciler` 权益对账兜底)。记 `LIVE_ACCOUNT_LEDGER_MODE = EXCHANGE_TRUTH_RECONCILIATION`。
- 实盘财务真相 = `ExchangeTruthReconciler`(本地 filled_quantity vs 交易所 myTrades)+
  `PositionReconciler`(持仓/权益 vs 交易所余额)+ `CrossReconciler`(本地内部一致性)→
  `ReconciliationMatrix` 统一处置; 跨源资金级差异(equity_drift/orphan_trade/fill_truth_*)
  单源即 KILLED。审计以 `tests/unit/test_v161_live_financial_truth.py` 钉死。
