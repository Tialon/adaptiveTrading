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
- 数据: MySQL `adaptive_trading` 库(19 张表, ORM 自动建表)

## 各运行模式

| 模式 | 命令 | 说明 |
|------|------|------|
| 完整交易(纸面) | `python run.py` | 默认 PAPER_TRADING=true, SOLUSDT |
| 完整交易(实盘) | `.env` 中 `PAPER_TRADING=false` + API Key | 建议先 testnet |
| 前端独立 | `python at10_web\web_serve_standalone.py [--port 9000]` | 只看面板/查库,不跑引擎 |
| 回测 | `python at70_backtest\backtest_run.py --symbol SOLUSDT --days 7` | 收益/胜率/回撤/夏普 |
| Walk-Forward | `from at70_backtest.backtest_walkforward import run_walkforward` | 过拟合检测 |
| 组合回测(真实管线) | `from at70_backtest.backtest_portfolio import run_portfolio_backtest` | 双仓+滑点敏感性(0/10/20bps), 含 win_rate/profit_factor/holding/sortino/calmar/attribution |
| 参数优化(实验) | `from at80_optimizer.optimizer import ParamOptimizer` | 候选生成→回测→落 strategy_versions→排序提案(不自动 activate) |
| 测试 | `.venv\Scripts\python -m pytest tests\ -v` | 340 个 |

## 配置速查(.env)

```ini
SYMBOLS=SOLUSDT              # 逗号分隔多标的
PAPER_TRADING=true           # 纸面模式(模拟成交)
DATABASE_URL=mysql+aiomysql://root:adaptive123@localhost:3306/adaptive_trading
REDIS_ENABLED=true           # Stream 事件总线(不可用自动降级)
BINANCE_TESTNET=true         # 先测试网!
AI_ENABLED=true              # AI 顾问(仅参数建议)
AI_PROVIDER=deepseek        # 供应商: openai/qwen/deepseek(默认 deepseek)
AI_INTERVAL_SECONDS=86400    # AI 调参周期(每日)
LIVE_TRADING_CONFIRM=true    # 主网实盘二次确认(非 testnet 必填, 否则启动拦截)
RECONCILE_INTERVAL_SECONDS=300  # 持仓对账周期
STARTUP_RECONCILE_ENABLED=true  # V10: 启动对账(仅实盘; 未解决差异 -> 急停冻结)
EQUITY_RECONCILE_TOLERANCE_PCT=0.02  # V10: 权益对账容差(本地 vs 交易所, 2%)
PORTFOLIO_CORE_RATIO=0.40    # V9: 组合三桶(核心/交易/现金, 和为 1.0)
PORTFOLIO_TRADING_RATIO=0.30
PORTFOLIO_CASH_RATIO=0.30
PORTFOLIO_REBALANCE_INTERVAL_SECONDS=300  # 核心仓低频决策周期
SELL_TAKE_PROFIT_LADDER=5:20,10:30,20:50   # V9 M2: 分批止盈阶梯(盈利%:卖出持仓%)
DAILY_REPORT_ENABLED=true    # V9: 每日自动复盘(reports/YYYY-MM-DD.md)
SLIPPAGE_REGIME_BPS=PANIC:50,VOLATILE:20,BEAR:15  # V9 M3: regime 条件滑点(命中放大)
REGIME_HMM_ENABLED=false     # V9 M3: HMM Regime(可选, 默认关; 不接实盘)
SENTIMENT_ENABLED=false      # V9 M3: Funding+OI 情绪因子(可选, 默认关)
```

## 数据库迁移(版本升级时)

ORM `create_all` 只建新表不改旧表,升级需手动 ALTER:

```sql
-- V2.0: signals 加 indicators(已执行于 2026-09-06)
ALTER TABLE signals ADD COLUMN indicators VARCHAR(2048) NULL;
-- V3.0: signal_result(已执行)
-- 完整结构见 at90_deploy/init.sql
```

> V8.0 新增 `trade_state` / `paper_state` 两张新表, 由 `create_all` 自动创建, 无需手动迁移。

> V9.0 新增 `trade_records` / `strategy_versions` 两张新表(共 17 张), 并给 `position_bucket`
> 追加 `target_ratio` / `target_quantity` / `current_value` 三列 —— 新库自动创建; 存量库需手动
> `ALTER TABLE position_bucket ADD COLUMN ...`(见 init.sql 演进)。

> V9.0 M3 新增 `account_ledger` 审计账本表(共 18 张), 由 `create_all` 自动创建, 无需手动迁移。

> V10.0 新增 `kill_switch_state` 急停状态表(单行 id=1, 共 19 张), 由 `create_all` 自动创建, 无需手动迁移。

> V10.3 新增 `position_lots` / `sell_allocations` 两张 Lot 会计表, 由 `create_all` 自动创建;
> 并给 `account_ledger` 追加 `realized_pnl` / `matched_cost` 两列 —— 新库自动创建, 存量库需手动
> `ALTER TABLE account_ledger ADD COLUMN realized_pnl FLOAT DEFAULT 0, ADD COLUMN matched_cost FLOAT DEFAULT 0`。

> V10.4 三维交叉对账(Order/Fill/Ledger/Lot)纯 DB 读, 无新表无迁移。

> V10.5 事件总线死信队列: 消费处理失败先重试 3 次(重入同流), 仍失败转 `<stream>:dlq`
> 死信队列(如 `at:market:events:dlq`), 不再静默丢弃。可 `XLEN <stream>:dlq` 观察积压。

> V10.5 交易规则过滤: 实盘下单前按 `/api/v3/exchangeInfo` 的 LOT_SIZE/PRICE_FILTER/
> MIN_NOTIONAL 对齐 stepSize/tickSize/minQty/minNotional, 违规本地拒绝(不投交易所)。
> 拉取失败自动降级为不过滤, 不影响下单。

> V10.5 断线回补: WS 断线重连前经 REST 重拉 K线/成交/盘口快照, 幂等合并补齐缺口
> 并刷新数据校验基线(避免误报 K线缺口)。回补失败仅记日志, 下次重连再试。

> V10.5 REDUCE_ONLY: 现货卖出执行前重读持仓封顶, 无持仓拒绝、超仓缩量, 关掉风控审批
> 到执行之间的竞态窗口; 订单落 `reduce_only` 标记(仅 SELL 为 1)。存量库需手动
> `ALTER TABLE orders ADD COLUMN reduce_only BOOLEAN DEFAULT 0`(新库自动创建)。

> V10.5 风险状态机: 风控暂停由隐式时间阈值改为显式 `NORMAL/PAUSED/KILLED` 三态
> (`at60_risk/risk_state.py`), 时间窗到期自动恢复、同因续期不重复告警, 每次进入
> PAUSED 落 `risk_events(event_type='risk_state')` 审计。无新表无迁移。

### AI 供应商切换(V9)

AI 顾问通过 `AI_PROVIDER` 选择供应商(`openai/qwen/deepseek`), 默认 `deepseek`,
各供应商的 API Key 与默认 `base_url` 在 `at50_strategy/llm_config.py` 中定义、从 `.env` 读取:

```ini
AI_PROVIDER=deepseek
# 通用覆盖(留空则用各供应商默认)
AI_BASE_URL=            # 例如自定义 OpenAI 兼容网关
AI_API_KEY=
# 各供应商 Key(.env 中配置, 代码不硬编码)
OPENAI_API_KEY=  QWEN_API_KEY=  DEEPSEEK_API_KEY=
```

协议: 三者统一走 OpenAI Chat Completions(`{base_url}/chat/completions`); base_url 默认各供应商自身端点。

### 可选模块(HMM / 情绪, 默认关闭)

```powershell
# HMM Regime 离线训练(独立 CLI, 不挂 run.py)
.venv\Scripts\python at30_analytics\regime_hmm_train.py --symbol SOLUSDT --days 30 --states 3
# 产出 models/regime_hmm.json; 需人工验证后手动开启 REGIME_HMM_ENABLED=true

# 情绪因子(合约 Funding+OI): .env 设 SENTIMENT_ENABLED=true 后随 run.py 低频轮询
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
| POST `/api/emergency/kill` | V10: 人工急停(冻结+撤全部未成交单, 持久化, 需 recover 解除) |
| POST `/api/emergency/recover` | V10: 解除急停(人工恢复交易) |
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
| 急停冻结(kill_switch armed) | 启动/权益对账未通过或人工急停触发; 核查日志与本地-交易所差异后 `POST /api/emergency/recover` 解除(重启不自动复位) |

## Pi 生产部署(已运行)

```bash
ssh root@pi
cd /opt/adaptiveTrading
docker compose logs -f adaptive-app     # 看日志
docker compose restart                  # 重启
docker compose up -d --build            # 更新代码后重建
# 面板: http://<pi-ip>:8800
```

架构: 复用 1panel-network 的 mysql8.2(root/mysql_EMtJnP, 库 adaptive_trading)
+ 1Panel-redis;容器 adaptive-app(纸面交易 SOLUSDT 测试网, restart=unless-stopped)。
代码同步: 本地打包 tar → scp → docker compose up -d --build。

## 停止

Ctrl+C 优雅停机(撤销 WS/关闭 DB/落盘缓冲)。
