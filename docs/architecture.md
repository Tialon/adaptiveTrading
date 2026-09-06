# 技术架构文档(V6.0)

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
        │  │  BULL / SIDEWAY / BEAR / PANIC        │      │
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
        │  │  MySQL (业务库)  +  Redis (Stream总线)  │      │
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
        │  │  30分钟周期, 只输出参数建议, 不交易      │      │
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
            ├──> Redis pub-sub (market:trade:{symbol})
            ├──> Redis Stream (at:market:events)     ← EventBus
            ├──> 批量缓冲 -> MySQL trades 表(5s批量)
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
| `at01_common` | 基础 | settings / database(惰性引擎) / logger / models(12表) |
| `at10_web` | 展示 | web_app / web_api_routes / web_ws_stream / web_state / web_serve_standalone / static |
| `at20_market` | 行情 | market_engine / market_models / market_rest_client / market_ws_client |
| `at30_analytics` | 分析 | engine / indicators / whale / accumulation / regime / alpha / bus(EventBus) |
| `at50_strategy` | 策略 | strategy_engine / strategy_base(Signal+source_strategy) / strategy_buy(entry) / strategy_sell(exit) / strategy_grid / strategy_trend / strategy_decision / strategy_identity(V6 枚举) / strategy_journal / strategy_signal_tracker / strategy_ai_advisor |
| `at50_execution` | 执行 | execution_executor / execution_paper_broker / execution_state(状态机) |
| `at60_risk` | 风控 | risk_manager / risk_position / risk_portfolio / risk_drawdown / risk_breaker / risk_allocation / risk_buckets / risk_tiered / risk_sizing / risk_ledger(V6 账本) |
| `at70_backtest` | 回测 | backtest_engine / backtest_run / backtest_walkforward / backtest_portfolio(V6 双仓+对账+分页数据) |
| `at90_deploy` | 部署 | Dockerfile / docker-compose / init.sql |

## 4. 数据库模型(12 张表)

| 表 | 用途 | 关键字段 |
|----|------|---------|
| klines | K线 | symbol+interval+open_time 唯一 |
| trades | 逐笔成交 | symbol+trade_id 唯一 |
| signals | 策略信号(V2+indicators) | score / reason / indicators JSON / status |
| orders | 订单 | client_order_id 唯一 / signal_id / strategy |
| positions | 持仓(账务) | avg_price / realized_pnl / peak_price |
| position_snapshot | V2 持仓快照 | equity / unrealized / realized(60s采样) |
| strategy_performance | V2 策略绩效 | win_rate / profit(strategy+symbol 唯一) |
| signal_result | V3 信号结果 | future_profit / max_profit / final |
| position_bucket | V4 双仓 | core/trade 独立数量+成本 |
| decision_log | V4 决策日志 | regime/置信/Alpha/双仓/现金 |
| ai_parameter_history | V5 AI 参数历史 | 旧值/新值/原因/效果 |
| risk_events / ai_advices | 风控事件 / AI 建议 | - |

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
