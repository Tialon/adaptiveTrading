# adaptiveTrading V9.0 — SOL Adaptive Swing Trader: 记忆交易实验平台 M1

> 定位: 把系统升级为「有记忆的交易实验平台」——沉淀每次判断/交易/环境/盈亏原因, 供 AI 优化。
> 冻结原则: Binance 单所 / SOLUSDT 单币 / 双仓 / 低频; AI 只优化不交易; 每步可回滚、兼容 paper、加测试加日志。

## 架构决策(已确认)

- **新建薄编排层**: `at55_portfolio/` + `at40_journal/` 复用 at60_risk / at50_strategy 原语, 不移动现有文件。
- **分里程碑**: 本轮只做 M1「记忆平台核心」; M2(Regime 6 态 + 策略整合 + AI Optimizer)另行规划。

## M1 交付

| # | 模块 | 文件 | 说明 |
|---|------|------|------|
| 1 | 组合三桶配置化 | `at01_common/settings.py`、`at60_risk/risk_allocation.py` | core/trading/cash 三比例; allocator 支持构造注入比例(默认沿用 0.70/0.30 向后兼容) |
| 2 | Portfolio Manager | `at55_portfolio/portfolio_manager.py` | 目标仓位 + evaluate() 偏离 + 目标落库 |
| 3 | Core Position Manager | `at55_portfolio/core_manager.py` | ADD/REDUCE/HOLD + Trend Break Protection |
| 4 | Trading Journal | `at40_journal/trading_journal.py` | SELL 成交闭环 -> `trade_records` |
| 5 | Strategy Version | `at50_strategy/strategy_version.py` | 参数快照 -> `strategy_versions` |
| 6 | 统一闸门 | `at60_risk/risk_manager.py` | `can_trade()` 合并熔断/异常保护 |
| 7 | 每日复盘 | `at40_journal/daily_report.py` | 聚合决策/成交/绩效 -> `reports/YYYY-MM-DD.md` |
| 8 | 接线 | `run.py` | `_portfolio_loop` + `_daily_report_loop` + `_on_signal` 闸门; 核心仓走 `bucket=core` |

## 关键复用(不重写)

- `PortfolioAllocator` / `BucketPositionManager` / `PositionManager` / `PortfolioEngine`(at60_risk)
- `MarketRegimeEngine`(at30_analytics, RegimeAssessment.btc_trend/regime)
- `ExecutionEngine`(at50_execution): 核心仓信号新增 `bucket=core` 绕过交易周期状态机, 新增 `on_trade_record` 回调

## 数据模型

- 新增表: `trade_records`(ClosedTrade)、`strategy_versions`(StrategyVersion) —— 全库 15 → 17 张。
- `position_bucket` 追加 `target_ratio` / `target_quantity` / `current_value` 三列。
- `PositionState` 追加 `entry_ts` / `trough_price`(内存态, 计算持仓时长/最大回撤)。

## 验证

- **235/235 测试通过**(单元 215 + 集成 20)
- 新增: `test_v9_portfolio.py`(16 个中的组合决策部分) / `test_v9_journal.py` / `test_v9_strategy_version.py`
- `import run` 与新模块导入冒烟通过
- 回测 `PortfolioBacktester` 行为不变(allocator 默认比例未变, 原 test_dual_bucket_split 仍绿)

## M2 交付

> 前置目的: 让 `at80_optimizer` 能跑起来 —— 可量化回测指标作目标函数、`strategy_versions` 作实验台账、统一策略分组作优化单位。

| # | 模块 | 文件 | 说明 |
|---|------|------|------|
| 1 | Regime 6 态 | `at30_analytics/regime.py` + 6 张系数表 | 中性区三档: NORMAL <1.0% / SIDEWAY 1.0~1.5% / VOLATILE ≥1.5%; BULL/BEAR/PANIC 判定不变 |
| 2 | 策略整合(薄分组层) | `at50_strategy/strategy_group.py` | 3 个组合策略伞: Trend Swing(trend+entry) / Mean Reversion(grid+entry) / Exit Manager(exit); 归因统一到伞名 |
| 3 | Exit Manager 统一 | `at50_strategy/strategy_sell.py` | 分批止盈阶梯 settings 化(`sell_take_profit_ladder`) |
| 4 | 版本快照分组 | `at50_strategy/strategy_version.py` | `group_params()` 按伞分组 + `snapshot()` 支持 `backtest_result` |
| 5 | 回测指标 6 项 | `at70_backtest/backtest_portfolio.py` | win_rate / profit_factor / holding / sortino / calmar / attribution; 闭环成交跟踪 |
| 6 | AI 优化器 | `at80_optimizer/` | 候选生成 → 回测评估 → 落库 → 排序提案(**不自动 activate**) |

### 关键复用(不重写)

- `PortfolioBacktester`(V7 真实管线)作评估器, `StrategyVersionManager` 作台账。
- 归因口径: `trade_records` / `strategy_performance` / 回测三处统一到 `group_of(source_strategy or strategy)`。

### 验证

- **280/280 测试通过**(M1 的 235 → +45)
- 新增: `test_v9_regime_6state` / `test_v9_strategy_group` / `test_v9_backtest_metrics` / `test_v9_optimizer`
- 冻结原则不变: 单所单币双仓低频, AI 只优化不交易, 每步可回滚、兼容 paper、加测试加日志。

## M3 交付

> 前置: 外部评审(7.6/10)逐条对照后, 大部分建议(V7 状态机/账本/自适应权重/Walk-Forward/滑点/手续费/复盘)已落地;
> 本里程碑补齐 5 项真正未落地且有价值者。

| # | 模块 | 文件 | 说明 |
|---|------|------|------|
| 1 | README 对齐 V9.0 | `README.md` | 修 V2.0 冻结、目录表补 3 包、6 态 + 3 伞、回测 6 指标、测试 303 |
| 2 | account_ledger 审计账本 | `at01_common/models.py` + `at60_risk/risk_account_ledger.py` + `execution_executor.py` | 每笔成交落 USDT + SOL 两行 before/change/after |
| 3 | Regime 条件滑点 | `backtest_execution.py` + `backtest_portfolio.py` + settings | `SlippageModel(regime_bps)` PANIC/VOLATILE/BEAR 放大 |
| 4 | HMM Regime(可选) | `at30_analytics/regime_hmm.py` + `regime_hmm_train.py` | 纯 Python 高斯 HMM, 默认关闭不接实盘 |
| 5 | Funding + OI 情绪(可选) | `at20_market/market_futures_client.py` + `at30_analytics/sentiment.py` | 情绪分, 默认关闭 |

### 验证

- **303/303 测试通过**(M2 280 → +23)
- 新增: `test_v9_account_ledger` / `test_v9_regime_slippage` / `test_v9_regime_hmm` / `test_v9_sentiment`
- 新增表 `account_ledger`(全库 17 → 18 张); 新增配置 slippage_regime_bps / regime_hmm_* / sentiment_*。
- 冻结不变: HMM / Funding-OI 默认关闭不接实盘 regime; 零新依赖; 单所单币双仓低频。
