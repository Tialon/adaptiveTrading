# adaptiveTrading V11.3 — SOL Adaptive Swing Trader

SOL/USDT 自动化量化交易系统:基于资金流/订单流/趋势状态的市场环境识别 + 双仓(核心/交易)低频摆动交易,
沉淀每次判断/交易/环境/盈亏原因,供 AI 长期优化。

> 原则: 规则策略实时交易 | AI 只分析与参数优化(不直接下单) | 风控优先 | 交易可解释 | 策略可回测
> 冻结: Binance 单所 / SOLUSDT 单币 / 双仓 / 低频

## 架构(V9.0)

```
Binance (WebSocket + REST)
        │
        ▼
Market Data Engine       成交/K线/盘口, 内存状态 + 落库 + Redis Stream 事件总线
        │
        ▼
Analytics Engine         VWAP / Delta / CVD / Whale / 吸筹 / EMA / OrderFlow(买卖压力/量比/大单占比)
        │
        ▼
Market Regime Engine     6 态: BULL / NORMAL / SIDEWAY / VOLATILE / BEAR / PANIC
                         (BTC+SOL 趋势 + 波动率 + 量能 + 资金流)
        │
        ▼
Strategy Engine          3 组合策略伞: Trend Swing / Mean Reversion / Exit Manager
                         Entry 评分模型(>=80买/60-80观察) + 分批止盈阶梯 + 移动止盈 + 趋势退出
                         │ 全部输出标准信号: score 0~100 + reason 列表 + indicators 快照
        ▼
Risk Engine              百分比风控(仓位40%/单笔5%/日亏5%/回撤15%) + 异常保护 + 统一交易闸门
                         (V10.5: 显式风险状态机 NORMAL/PAUSED/KILLED + 急停持久化)
                         (V11.2: 顶层 SystemLifecycle 状态机 + TradingGate 六维闸门 + 资金级熔断)
        │
        ▼
Portfolio Manager        核心/交易/现金三桶(配置驱动) + Core Manager(ADD/REDUCE/HOLD)
        │
        ▼
Execution Engine         幂等下单 + 纸面(默认)/实盘轮询成交 + 状态机(防重复建仓)
                         (V10.5: ExchangeInfo 规则过滤 + REDUCE_ONLY + 三维交叉对账)
        │
        ├── Trading Journal (trade_records 成交闭环)
        ├── Strategy Version (strategy_versions 参数快照)
        ├── Optimizer (at80_optimizer 网格搜索 → 提案, 不自动激活)
        └── Account Ledger (account_ledger 逐笔余额变更审计)
        │
        ▼
SQLite(默认)/MySQL(生产) + Redis(可选) + AI Advisor(仅参数建议: grid_spacing/position_ratio/risk)
        │
        ▼
Web Dashboard             http://localhost:8800 (REST + WS 推送)
```

## 快速开始

```powershell
# (可选)生产用 MySQL/Redis: 否则默认 SQLite 零依赖, 跳过本步
cd at90_deploy; docker compose up -d mysql redis; cd ..

# 运行(纸面交易, 默认 SOLUSDT)
.venv\Scripts\python run.py

# 回测(真实策略管线, 次bar执行 + 滑点)
.venv\Scripts\python at70_backtest\backtest_run.py --symbol SOLUSDT --days 7

# 测试
.venv\Scripts\python -m pytest tests/ -v
```

## 回测输出

收益率 / 胜率 / 最大回撤 / 夏普比率 / 交易次数
+ win_rate / profit_factor / avg_holding / sortino / calmar / attribution(按策略伞归因)。

## 目录结构

| 目录 | 包名 | 职责 |
|------|------|------|
| `at01_common/` | `common` | 配置(含 `validate()` 启动审计)/ 日志 / 数据库(SCHEMA_VERSION, 25 张表)/ ORM 模型 |
| `at10_web/` | `web` | FastAPI + WS + 面板(/api/regime /api/equity-curve /api/strategy-performance) |
| `at20_market/` | `market` | REST/WS 客户端 + 行情引擎 + 事件总线 |
| `at30_analytics/` | `analytics` | 指标 / OrderFlow / MarketRegimeEngine(6 态) |
| `at40_journal/` | `journal` | Trading Journal(trade_records) + 每日复盘报告 |
| `at50_strategy/` | `strategy` | Entry 评分 / Exit 阶梯 / 网格 / 趋势 + 策略分组 + 版本快照 |
| `at50_execution/` | `execution` | 幂等执行 + 状态机 + 纸面/实盘 + 审计账本接线 |
| `at55_portfolio/` | `portfolio` | 组合编排薄层(Portfolio Manager / Core Manager) |
| `at60_risk/` | `risk` | 百分比风控 + 异常保护 + 双仓账本(PortfolioLedger) + 账户审计账本 |
| `at70_backtest/` | `backtest` | 回测引擎(真实策略管线 + 次bar执行 + 滑点 + Walk-Forward) |
| `at80_optimizer/` | `optimizer` | 参数优化(网格搜索 → 回测 → 落库 → 排序提案) |
| `at90_deploy/` | - | Dockerfile / docker-compose / init.sql |

## API 摘要

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/system` | 系统总览(含 regime) |
| GET | `/api/market` `/api/analytics` | 行情/分析快照 |
| GET | `/api/regime` | 市场环境评估 |
| GET | `/api/positions` `/api/risk` | 持仓/风控 |
| GET | `/api/signals` `/api/orders` | 信号(score/indicators)/订单 |
| GET | `/api/strategy-performance` | 策略胜率/收益 |
| GET | `/api/equity-curve` | 收益曲线(position_snapshot) |
| GET | `/api/metrics` | 可观测性指标(snapshot + alerts + 策略归因) |
| POST | `/api/breaker/reset` | 解除熔断 |
| WS | `/ws` | 实时推送(2s) |

## V9.0 策略与市场环境

**Market Regime(6 态)**: BULL(趋势向上+资金流入) / NORMAL(平静) / SIDEWAY(中性盘整) /
VOLATILE(宽幅震荡) / BEAR(趋势向下+资金流出) / PANIC(剧烈波动+放量)。

**组合策略伞(3 个, 归因统一到伞名)**:
- Trend Swing(trend + entry): 趋势跟随 + 评分买入
- Mean Reversion(grid + entry): 网格高抛低吸 + 评分买入
- Exit Manager(exit): 分批止盈阶梯 + 移动止盈 + 趋势退出

**Entry 评分模型**(5 维加权): 价格位置 30% + VWAP 偏离 20% + CVD 20% + 主动买卖比 15% + 量能变化 15%
(`>= 80` 买入 / `60~80` 观察档 / `< 60` 禁止)

**Exit 分批止盈阶梯**(settings 化 `sell_take_profit_ladder`): 盈利 5% 卖 20% / 10% 卖 30% / 20% 卖 50%
**移动止盈**: 峰值回撤 5% 清仓; **趋势退出**: EMA 死叉 + CVD 降 + 买压减(三中二)

**Regime 策略调整**: BULL 趋势为主 / SIDEWAY 网格高抛低吸 / BEAR 停止补仓 / PANIC 只减不加。

## 双仓模型(核心 / 交易 / 现金)

- 三桶比例配置驱动(`portfolio_core/trading/cash_ratio`, 默认 0.40/0.30/0.30)。
- 核心仓: 低频 ADD/REDUCE/HOLD + Trend Break Protection(EMA 死叉 / BTC 锚失败 / PANIC)。
- 交易仓: 高频摆动(网格/评分);卖出只动交易仓, 不碰核心仓。

## 风控(默认)

- 持仓 ≤ 权益 40%; 单笔 ≤ 权益 5%; 日亏 5% 熔断; 回撤 15% 熔断; 冷却 300 秒
- 异常保护: 单笔价格波动 >3% 暂停 / 行情静默 >30 秒暂停 / 连续 3 次执行失败暂停
- 统一交易闸门(`TradingGate` 六维: 生命周期 + 风险态 + 行情健康 + 交易所健康 + 对账 + 资金熔断): 单一权威, 一切开仓/减仓/撤单必过此闸门

风险自负: 实盘前请在 testnet + paper 模式充分验证(当前回测结论暂不建议实盘)。

## 工程文档(docs/)

| 文档 | 内容 |
|------|------|
| [architecture.md](docs/architecture.md) | 技术架构图 / 数据流程图 / 模块分层 / 数据库模型 / 设计决策 |
| [trading-logic.md](docs/trading-logic.md) | 交易逻辑: Entry评分 / Exit标签 / 融合决策 / 风控链 / 成本管理 |
| [module-map.md](docs/module-map.md) | 代码地图: 每个文件职责速查 |
| [runbook.md](docs/runbook.md) | 运行手册: 启动/配置/API/迁移/排障 |
| [production-readiness.md](docs/production-readiness.md) | 生产就绪检查清单(V11.3 加固核对) |
| [metrics-persistence.md](docs/metrics-persistence.md) | Metrics 持久化方案评估(保持内存) |
| [progress.md](docs/progress.md) | 进度日志: V1→V11.3 交付与验证记录 |
| [cc_task_v11_2.md](cc_task_v11_2.md) | V11.2 任务清单(P0/P1)+ 集成/验收/审计记录 |
| [cc_task_v11.md](cc_task_v11.md) | V11.0 任务清单(P0/P1/P2)+ 深度审计修复记录 F1-F13 |
| [cc_task_v9.md](cc_task_v9.md) | V9 需求任务清单(勾选状态) |
