# 运行手册

## 环境

| 依赖 | 要求 |
|------|------|
| Python | 3.11+(开发验证 3.13) |
| Docker | MySQL 8 + Redis 7(本机) |
| 网络 | 币安测试网/主网 + AI 网关(可选) |

## 快速启动

```powershell
# 1. 虚拟环境(一次性)
python -m venv .venv
.venv\Scripts\activate
pip install aiohttp "sqlalchemy[asyncio]" aiomysql aiosqlite cryptography redis `
    pydantic pydantic-settings python-dotenv fastapi "uvicorn[standard]" structlog websockets `
    pytest pytest-asyncio

# 2. 基础设施(本机 Docker)
cd at90_deploy; docker compose up -d mysql redis; cd ..

# 3. 配置
copy .env.example .env   # 填入币安 Key / AI Key

# 4. 运行
.venv\Scripts\python run.py
```

启动后:
- 面板: http://localhost:8800
- 日志: logs/adaptive.log(JSON)
- 数据: MySQL `adaptive_trading` 库(9 张表, ORM 自动建表)

## 各运行模式

| 模式 | 命令 | 说明 |
|------|------|------|
| 完整交易(纸面) | `python run.py` | 默认 PAPER_TRADING=true, SOLUSDT |
| 完整交易(实盘) | `.env` 中 `PAPER_TRADING=false` + API Key | 建议先 testnet |
| 前端独立 | `python at10_web\web_serve_standalone.py [--port 9000]` | 只看面板/查库,不跑引擎 |
| 回测 | `python at70_backtest\backtest_run.py --symbol SOLUSDT --days 7` | 收益/胜率/回撤/夏普 |
| Walk-Forward | `from at70_backtest.backtest_walkforward import run_walkforward` | 过拟合检测 |
| 测试 | `.venv\Scripts\python -m pytest tests\ -v` | 140 个(单测+集成) |

## 配置速查(.env)

```ini
SYMBOLS=SOLUSDT              # 逗号分隔多标的
PAPER_TRADING=true           # 纸面模式(模拟成交)
DATABASE_URL=mysql+aiomysql://root:adaptive123@localhost:3306/adaptive_trading
REDIS_ENABLED=true           # Stream 事件总线(不可用自动降级)
BINANCE_TESTNET=true         # 先测试网!
AI_ENABLED=true              # AI 顾问(仅参数建议)
```

## 数据库迁移(版本升级时)

ORM `create_all` 只建新表不改旧表,升级需手动 ALTER:

```sql
-- V2.0: signals 加 indicators(已执行于 2026-09-06)
ALTER TABLE signals ADD COLUMN indicators VARCHAR(2048) NULL;
-- V3.0: signal_result(已执行)
-- 完整结构见 at90_deploy/init.sql
```

## API 速查

| 端点 | 说明 |
|------|------|
| GET `/api/system` | 总览(权益/风控/regime/状态机) |
| GET `/api/market` `/api/analytics` `/api/regime` | 行情/分析/环境 |
| GET `/api/signals` | 信号(score/reason/indicators) |
| GET `/api/equity-curve` | 收益曲线(position_snapshot) |
| GET `/api/strategy-performance` | 策略胜率 |
| POST `/api/breaker/reset` | 手动解除熔断 |
| WS `/ws` | 2s 推送(market/analytics/risk/regime/execution) |

## 排障

| 症状 | 处理 |
|------|------|
| `pydantic_core._pydantic_core` 缺失 | `pip install --force-reinstall --no-cache-dir pydantic-core` |
| MySQL 认证失败(caching_sha2) | `pip install --no-cache-dir cryptography` |
| 面板 WS 连不上 | `pip install websockets`(uvicorn 需要) |
| 建表失败 `no such table` | 首次运行自动建;检查 DATABASE_URL |
| 行情静默告警频繁 | WS 断线,自动重连中;检查网络/代理 |
| 熔断 OPEN | 回撤≥15% 或日亏≥5%,冷却 300s 后自动恢复或 POST 解除 |

## 停止

Ctrl+C 优雅停机(撤销 WS/关闭 DB/落盘缓冲)。
