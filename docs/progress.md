# 项目进度日志

> 记录每个开发阶段的关键交付与验证结论

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
