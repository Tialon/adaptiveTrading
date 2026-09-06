# adaptiveTrading 自适应交易系统

币安(Binance)现货自适应交易系统:行情 → 分析 → 策略 → 风控 → 执行 全链路异步实现,带 Web 监控面板。

## 架构

```
Binance (WebSocket + REST)
        │
        ▼
Market Data Engine      成交/K线/盘口/24h行情,内存状态 + 落库 + Redis 发布(可选)
        │
        ▼
Analytics Engine        VWAP / Delta / CVD / 大单检测(Whale) / 吸筹检测(Accumulation) / EMA趋势
        │
        ▼
Strategy Engine         买入(吸筹折价) / 卖出(止盈+移动止盈) / 网格 / 趋势(EMA金叉死叉)
        │
        ▼
Risk Engine             仓位限额 / 单笔限额 / 最大回撤 / 日内亏损 / 熔断冷却
        │
        ▼
Execution Engine        纸面交易(默认) / 实盘限价单 + 轮询成交确认 + 重试
        │
        ▼
MySQL/SQLite + Redis  +  AI Advisor(OpenAI 兼容接口,可选)
        │
        ▼
Web 监控面板            http://localhost:8800  (REST + WebSocket 实时推送)
```

## 目录结构

| 目录 | 包名 | 职责 |
|------|------|------|
| `00_common/` | `common` | 配置 / 日志 / 数据库 / ORM 模型 |
| `10_web/` | `web` | FastAPI + WebSocket + 静态面板 |
| `20_market/` | `market` | REST 客户端 / WS 客户端(自动重连) / 行情引擎 |
| `30_ayalytics/` | `analytics` | 指标 / 大单 / 吸筹 / 分析引擎 |
| `50_startegy/` | `strategy` | 策略基类 / 四策略 / AI 顾问 / 策略引擎 |
| `50_execution/` | `execution` | 执行引擎 / 纸面 Broker |
| `60_risk/` | `risk` | 持仓 / 回撤 / 熔断 / 风控管理器 |
| `90_deploy/` | - | Dockerfile / docker-compose / init.sql |

## 快速开始

### 本地运行(纸面交易,SQLite,零外部依赖)

```bash
# 1. 创建虚拟环境并安装依赖
python -m venv .venv
.venv\Scripts\activate           # Windows
pip install -e ".[dev]"          # 或 pip install aiohttp sqlalchemy aiosqlite pydantic-settings fastapi uvicorn structlog redis

# 2. 运行(默认 PAPER_TRADING=true,SQLite)
python run.py
```

打开 http://localhost:8800 查看监控面板。

### Docker 部署(MySQL + Redis)

```bash
cd 90_deploy
docker compose up -d
```

`.env` 配置参考 `.env.example`。

### 切换实盘

```env
PAPER_TRADING=false
BINANCE_TESTNET=true            # 先在测试网验证
BINANCE_TESTNET_API_KEY=...
BINANCE_TESTNET_API_SECRET=...
```

## API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 监控面板 |
| GET | `/api/system` | 系统总览 |
| GET | `/api/market` | 行情快照 |
| GET | `/api/analytics` | 分析快照(VWAP/CVD/吸筹等) |
| GET | `/api/risk` | 风控状态 |
| GET | `/api/positions` | 持仓 |
| GET | `/api/orders` | 近期订单 |
| GET | `/api/signals` | 近期信号 |
| POST | `/api/breaker/reset` | 解除熔断 |
| WS | `/ws` | 实时推送(2s) |

## 测试

```bash
.venv\Scripts\python -m pytest tests/ -v      # 全部 75 个(55 单元 + 20 集成)

tests/unit/         # 指标 / 风控 / 策略 / 纸面交易(纯内存,秒级)
tests/integration/  # WS 消息路由(真实币安消息格式) / Web API(TestClient)
```

## 风控参数(默认)

- 单标的最大持仓 20,000 USDT;单笔最大 2,000 USDT
- 最大回撤 10% 触发熔断,冷却 300 秒
- 日内亏损 5% 触发熔断

风险自负:实盘前请先在 testnet + paper 模式充分验证。
