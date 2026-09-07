# 项目进度日志

> 记录每个开发阶段的关键交付与验证结论

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
