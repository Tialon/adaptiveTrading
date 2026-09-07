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

## M2(未做, 另行规划)

- Regime 6 态扩展(NORMAL/VOLATILE), 跨 6+ 文件与 AI prompt
- 策略整合为 3 个(Trend Swing / Mean Reversion / 统一 Exit Manager)
- `at80_optimizer`(AI 优化器 / 实验管理)
- 回测指标补齐(win_rate / profit_factor / holding_period / sortino / calmar / attribution)
