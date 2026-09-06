# adaptiveTrading V2.0 — SOL 专业量化交易系统

SOL/USDT 自动化量化交易系统:基于资金流/订单流/趋势状态的动态高抛低吸,持续降低持仓成本。

> 原则: 规则策略实时交易 | AI 只分析与参数优化 | 风控优先 | 交易可解释 | 策略可回测

## 架构(V2.0)

```
Binance (WebSocket + REST)
        │
        ▼
Market Data Engine      成交/K线/盘口,内存状态 + 落库 + Redis Stream 事件总线
        │
        ▼
Analytics Engine        VWAP / Delta / CVD / Whale / 吸筹 / EMA / OrderFlow(买卖压力/量比/大单占比)
        │
        ▼
Market Regime Engine    BULL / SIDEWAY / BEAR / PANIC(BTC+SOL趋势+波动率+量能+资金流)
        │
        ▼
Strategy Engine         Entry评分模型(>=80买/60-80观察) / Exit(分批止盈+移动止盈+趋势退出) / 网格 / 趋势
        │                全部输出标准信号: score 0~100 + reason列表 + indicators快照
        ▼
Position Manager        持仓成本/可卖数量/可买额度 + 定时快照(position_snapshot)
        │
        ▼
Risk Engine             百分比风控(仓位40%/单笔5%/日亏5%/回撤15%) + 异常保护(价格波动/行情静默/连续失败)
        │
        ▼
Execution Engine        幂等下单(信号去重) + 纸面交易(默认) / 实盘轮询成交确认
        │
        ▼
MySQL + Redis + AI Advisor(仅参数建议: grid_spacing/position_ratio/risk)
        │
        ▼
Web Dashboard           http://localhost:8800 (REST + WS 推送: 策略评分/市场环境/风控/收益)
```

## 快速开始

```powershell
# 本机 Docker 起基础设施
cd at90_deploy; docker compose up -d mysql redis; cd ..

# 运行(纸面交易,默认 SOLUSDT)
.venv\Scripts\python run.py

# 回测
.venv\Scripts\python at70_backtest\backtest\run.py --symbol SOLUSDT --days 7

# 测试
.venv\Scripts\python -m pytest tests/ -v
```

## 回测输出

收益率 / 胜率 / 最大回撤 / 夏普比率 / 交易次数 + 交易明细。

## 目录结构

| 目录 | 包名 | 职责 |
|------|------|------|
| `at01_common/` | `common` | 配置 / 日志 / 数据库 / ORM 模型(V2.0: +position_snapshot/strategy_performance, signals+indicators) |
| `at10_web/` | `web` | FastAPI + WS + 面板(V2.0: /api/regime /api/equity-curve /api/strategy-performance) |
| `at20_market/` | `market` | REST/WS 客户端 + 行情引擎(V2.0: 事件总线发布) |
| `at30_analytics/` | `analytics` | 指标/大单/吸筹 + V2.0: OrderFlow / MarketRegimeEngine / EventBus(Redis Stream) |
| `at50_strategy/` | `strategy` | V2.0: Entry 评分 / Exit 分批止盈 / 网格 / 趋势 + AI 参数顾问(不交易) |
| `at50_execution/` | `execution` | V2.0: 幂等执行(信号去重) + 策略绩效落库 |
| `at60_risk/` | `risk` | V2.0: 百分比风控 + 异常保护 + 持仓快照/可买可卖额度 |
| `at70_backtest/` | `backtest` | V2.0: 回测引擎(历史K线回放) |
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
| POST | `/api/breaker/reset` | 解除熔断 |
| WS | `/ws` | 实时推送(2s) |

## V2.0 策略参数

**Entry 评分模型**(5 维加权):
- 价格位置 30% + VWAP 偏离 20% + CVD 20% + 主动买卖比 15% + 量能变化 15%
- `>= 80` 买入 / `60~80` 观察档(仅记录) / `< 60` 禁止

**Exit 分批止盈阶梯**: 盈利 5% 卖 20% / 10% 卖 30% / 20% 卖 50%
**移动止盈**: 峰值回撤 5% 清仓; **趋势退出**: EMA死叉+CVD降+买压减(三中二)

**Market Regime 策略调整**: BULL 趋势为主 / SIDEWAY 网格高抛低吸 / BEAR 停止补仓 / PANIC 只减不加

## 风控(默认)

- 持仓 ≤ 权益 40%; 单笔 ≤ 权益 5%; 日亏 5% 熔断; 回撤 15% 熔断; 冷却 300 秒
- 异常保护: 单笔价格波动 >3% 暂停 / 行情静默 >30 秒暂停 / 连续 3 次执行失败暂停

风险自负: 实盘前请在 testnet + paper 模式充分验证。
