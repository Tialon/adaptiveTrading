# adaptiveTrading V12.4 — SOL Adaptive Swing Trader

SOL/USDT 自动化量化交易系统:基于资金流/订单流/趋势状态的市场环境识别 + 双仓(核心/交易)低频摆动交易,
沉淀每次判断/交易/环境/盈亏原因,供 AI 长期优化。

> 原则: 规则策略实时交易 | AI 只分析与参数优化(不直接下单) | 风控优先 | 交易可解释 | 策略可回测
> 冻结: Binance 单所 / SOLUSDT 单币 / **现货(非合约)** / 双仓 / 低频
>
> **接手代码先读 [`CLAUDE.md`](CLAUDE.md)** —— 红线、阅读路线、常用命令都在那一页。
> 文档索引见 [`docs/README.md`](docs/README.md)。

## 架构

> 📖 **完整架构图见 [`docs/architecture.md`](docs/architecture.md)** —— 分层总图 + 成交时序图 +
> 四套状态机关系图。README 不再内嵌一份(内嵌的那份曾停留在 V9.0, 与代码脱节后没人发现)。

**编号即阅读顺序 = 数据流顺序**（十位是层号，个位 `0`=主 / `5`=同层辅助）：

```
L0  at01_common      基础(横切)  配置审计 / ORM(28 表) / 惰性引擎 / 迁移 / 任务监督 / 就绪自检
L1  at10_market      行情接入    WS 重连 / 状态预热 / 数据校验
L2  at20_analytics   分析        VWAP / CVD / Whale / 吸筹 / regime(6 态) / alpha
L3  at30_strategy    策略        Entry 评分(≥80买/60-80观察) / Exit 阶梯 / 多策略加权融合
L4  at40_portfolio   组合        核心/交易/现金三桶 + Core Manager(ADD/REDUCE/HOLD)
L5  at50_risk        风控        TradingGate(六维+两维) / SystemLifecycle(10 态) / 资金熔断
L6  at60_execution   执行        幂等下单 / 交易状态机 / 纸面(默认)或实盘 / 对账 / 账本
L7  at70_journal     记录        交易日志 / 每日复盘 / HODL 对标
L8  at80_backtest    研究        回测(真实策略管线 + 次 bar + 滑点) / Walk-Forward
L8  at85_optimizer   研究        参数优化 → 只产提案, 不自动激活
L9  at90_web         展示(横切)  REST / WS / 面板 / 管理控制台 / 部署自检页
    run.py           编排层      AdaptiveTradingSystem —— 唯一编排器

存储: SQLite + WAL(默认, 28 张表)  ·  Redis 可选(默认关)  ·  AI Advisor 只给参数建议
```

## 快速开始

```powershell
# 运行(纸面交易, 默认 SOLUSDT)
.venv\Scripts\python run.py

# Docker 生产运行(单容器 + SQLite 持久化, 见 docs/docker-deployment.md)
docker compose up -d

# 回测(真实策略管线, 次bar执行 + 滑点)
.venv\Scripts\python at80_backtest\backtest_run.py --symbol SOLUSDT --days 7

# 测试(CI 同款: 排除 testnet 真实交易, 覆盖率 ≥ 75%)
uv run pytest -q --cov --cov-report=term-missing --cov-fail-under=75 -m "not testnet"
```

## 回测输出

收益率 / 胜率 / 最大回撤 / 夏普比率 / 交易次数
+ win_rate / profit_factor / avg_holding / sortino / calmar / attribution(按策略伞归因)。

## 目录结构

| 目录 | 包名 | 职责 |
|------|------|------|
| `at01_common/` | `common` | 配置(含 `validate()` 启动审计)/ 日志 / 数据库(SCHEMA_VERSION, 28 张表)/ ORM 模型 |
| `at90_web/` | `web` | FastAPI + WS + 面板(/api/regime /api/equity-curve /api/strategy-performance) |
| `at10_market/` | `market` | REST/WS 客户端 + 行情引擎 + 事件总线 |
| `at20_analytics/` | `analytics` | 指标 / OrderFlow / MarketRegimeEngine(6 态) |
| `at70_journal/` | `journal` | Trading Journal(trade_records) + 每日复盘报告 |
| `at30_strategy/` | `strategy` | Entry 评分 / Exit 阶梯 / 网格 / 趋势 + 策略分组 + 版本快照 |
| `at60_execution/` | `execution` | 幂等执行 + 状态机 + 纸面/实盘 + 审计账本接线 |
| `at40_portfolio/` | `portfolio` | 组合编排薄层(Portfolio Manager / Core Manager) |
| `at50_risk/` | `risk` | 百分比风控 + 异常保护 + 双仓账本(PortfolioLedger) + 账户审计账本 |
| `at80_backtest/` | `backtest` | 回测引擎(真实策略管线 + 次bar执行 + 滑点 + Walk-Forward) |
| `at85_optimizer/` | `optimizer` | 参数优化(网格搜索 → 回测 → 落库 → 排序提案) |
| `Dockerfile` / `docker-compose.yml` | - | V11.8 生产运行时(多阶段 uv + tini PID1 + 非 root + SQLite 持久化卷)。**必须留在根目录**: compose 卷用相对路径, 见 [`deploy/README.md`](deploy/README.md) |
| `deploy/` | - | 部署资产(`init.sql` 建库 / `pi/production.env.example` 模板), 不含 Dockerfile |

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
| GET | `/api/metrics` | 可观测性指标(snapshot + alerts + 策略归因 + `health` 运行时健康快照 V11.5) |
| GET | `/api/operator-status` | **操作者状态聚合(只读)**: 模式/状态/买卖许可/人话结论/下一步/开关解释 |
| GET | `/ops` | **部署检查页(只读)**: PASS/WARN/BLOCKED 三态, 对应上线前检查步骤 |
| GET | `/api/admin/config` | **配置摘要 + 可编辑字段定义**(只读, 敏感项只报已配置/未配置) |
| POST | `/api/admin/config/draft` | 校验配置草稿, 返回 diff / 风险 / 是否需重启(**不写文件**, 🔒) |
| POST | `/api/admin/config/apply` | 写入配置文件(写前备份, 原子替换; **不热生效**, 🔒) |
| POST | `/api/admin/config/rollback` | 恢复最近一份配置备份(🔒) |
| GET | `/api/admin/auth-check` | 令牌校验探针(🔒, 供页面显示令牌状态) |
| POST | `/api/admin/restart` | 重启服务使配置生效(重启前先过启动守卫, 🔒) |

> **写接口鉴权**: 默认 `WEB_ADMIN_AUTH=on`, 所有 🔒 接口需要 `X-Admin-Token`。
> 个人局域网单用户可显式设 `WEB_ADMIN_AUTH=off` 关闭 —— 此时页面无需令牌,
> 但**局域网内任何设备都能改配置/恢复急停/停机**; 关闭期间启动日志、页面、`/ops` 均常驻告警。
> 详见 [runbook.md](docs/runbook.md) §4.5。
| POST | `/api/breaker/reset` | 解除熔断(🔒 需 X-Admin-Token) |
| POST | `/api/emergency/kill` `/api/emergency/recover` `/api/shutdown` | 急停/恢复/停机(🔒 需 X-Admin-Token) |

### Web 三个入口

| 入口 | 用途 | 是否可写 |
|------|------|----------|
| `/` | **看状态** —— 运行结论卡、行情/分析/风控/持仓、数据库记录 | 仅急停/恢复/解除熔断/停机等管理动作 |
| `/admin` | **改配置 / 切模式 / 管理操作** —— 模式选择、开关、参数、草稿 diff、保存与回滚 | ✅ 需 `X-Admin-Token` |
| `/ops` | **上线前只读自检** —— PASS / WARN / BLOCKED 三态 | ❌ 纯只读, 无任何危险按钮 |

`/admin` 的配置改动走「草稿 → 预览 → 保存」三段式: 预览只校验并展示 diff 与风险提示, **不写文件**;
保存才写配置(**写前自动备份**), 且**只落盘不热生效**, 页面会明确提示重启命令。
`/ops` 的入口在 `/admin` 与 `/` 顶部都有, 部署前先跑一遍。

### Web Dashboard 使用说明

打开 `http://localhost:8800` 后, **首屏顶部的运行结论卡**直接回答四个问题, 不需要读 `.env`:

1. **当前是什么模式** —— 纸面+测试网 / 测试网真实下单 / 纸面+主网行情 / **主网真实资金**(红色)。
2. **能不能交易** —— 买入许可、卖出许可分别显示允许/禁止, 数据直接来自 `TradingGate`(单一权威)。
3. **为什么不能** —— 显示阻断原因(`buy_block_reason` 优先), 以及「下一步该做什么」的人话建议。
4. **写操作是否可用** —— 未配置 `WEB_ADMIN_TOKEN` 时显示「未启用」, 急停按钮置灰并说明原因。

配套说明:

- **当前配置解释卡** 把 `PAPER_TRADING` / `BINANCE_TESTNET` / `LIVE_TRADING_CONFIRM` /
  `MAINNET_API_SCOPE_CONFIRM` / `WEB_ADMIN_TOKEN` 翻译成人话; 令牌只显示「已配置/未配置」, **不回显值**。
- **危险写操作** 需二次确认: 「恢复急停」「解除熔断」「停机」会弹出确认框, 列出当前模式、当前状态与操作后果;
  主网真实资金模式下确认文案会显式点明。**急停**不设确认(冻结是安全方向, 应即时可用)。
- **令牌输入框** 在右上角, 只存在浏览器 `sessionStorage`, 不上传、不写入 URL。
- 写操作失败会**显示服务端返回的 `detail`/`msg`**, 不会静默失败; 成功后自动刷新结论卡与指标快照。
- 状态一律以中文为主提示, 英文状态(`TRADING`/`KILLED`…)保留为小字 badge 便于排查。

> 状态含义与各模式运维见 [operating-modes-manual.md](docs/operating-modes-manual.md)。
| WS | `/ws` | 实时推送(2s) |

> Web 安全(V11.5): 默认 `API_HOST=127.0.0.1`; 写接口统一 `X-Admin-Token` 头鉴权(`WEB_ADMIN_TOKEN` 空则锁定)。
> GET 查询接口无需鉴权。

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
- 统一交易闸门(`TradingGate`): 单一权威, 一切开仓/减仓/撤单必过此闸门。
  名义「六维」(生命周期 + 风险态 + 行情健康 + 交易所健康 + 对账 + 资金熔断),
  V11.6 的 BUY 安全契约又收了两维(停机窗口 + 关键后台任务健康), **实为 6+2 共八项**。

风险自负: 实盘前请在 testnet + paper 模式充分验证(当前回测结论暂不建议实盘)。

## 工程文档(docs/)

| 文档 | 内容 |
|------|------|
| [architecture.md](docs/architecture.md) | 技术架构图 / 数据流程图 / 模块分层 / 数据库模型 / 设计决策 |
| [trading-logic.md](docs/trading-logic.md) | 交易逻辑: Entry评分 / Exit标签 / 融合决策 / 风控链 / 成本管理 |
| [module-map.md](docs/module-map.md) | 代码地图: 每个文件职责速查 |
| [runbook.md](docs/runbook.md) | 运行手册: 启动/配置/API/迁移/排障 |
| [operating-modes-manual.md](docs/operating-modes-manual.md) | 运行模式手册: 纸面/测试网真实/主网 三模式判据 + 启动守卫链 + 状态含义 + 运维动作速查 |
| [production-readiness.md](docs/production-readiness.md) | 生产就绪检查清单 + 就绪等级(L2 运行时验证就绪) |
| [metrics-persistence.md](docs/metrics-persistence.md) | Metrics 持久化方案评估(保持内存) |
| [progress.md](docs/progress.md) | 进度日志: V1→V12 交付与验证记录 |
| [testnet-runbook.md](docs/testnet-runbook.md) | 测试网无人值守运维手册(soak 启动/监控/证据/停机) |
| [testnet-operation.md](docs/testnet-operation.md) | 测试网验证状态(做到哪/诚实结论/续跑步骤) |
| [docker-deployment.md](docs/docker-deployment.md) | Docker 生产部署(构建/启动/备份/升级回滚/镜像站覆盖) |
| [raspberry-pi-deployment.md](docs/raspberry-pi-deployment.md) | 树莓派(arm64)生产部署 + 无人值守自检 |
| [mainnet-runbook.md](docs/mainnet-runbook.md) | 主网运维手册(极小资金真实交易, 冻结红线) |
| [mainnet-readiness.md](docs/mainnet-readiness.md) | 主网就绪自检 + 人工上线复审 go/no-go 清单 |
| [mainnet-prestart-checklist.md](docs/mainnet-prestart-checklist.md) | 每次主网启动前的备份、配置、接管与 go/no-go 清单 |
| [production.env.example](deploy/pi/production.env.example) | Pi 局域网生产部署的外置环境文件模板（密钥与源码分离） |
| [database-migration.md](docs/database-migration.md) | 数据库迁移单一入口(前向 DDL 框架 + 迁移历史) |
| [tasks/](docs/tasks/) | **历史工单归档**(18 份 cc_task_*.md): 每个版本的任务清单与当时的执行记录。⚠️ 归档件中的包名是**当时的旧名**, 映射见 [tasks/README.md](docs/tasks/README.md) |
| [design/](design/) | 设计图(⚠️ V1.0 时期存档, 已与代码脱节; 当前架构图以 [architecture.md](docs/architecture.md) 为准) |
