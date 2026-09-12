# 运行手册

## 环境

| 依赖 | 要求 |
|------|------|
| Python | 3.11+(开发验证 3.13) |
| Docker | 生产运行时(V11.8 起: 单容器 + SQLite 持久化卷; 本地开发仍可裸 `python run.py` 零依赖) |
| 网络 | 币安测试网/主网 + AI 网关(可选) |

## 快速启动

```powershell
# 1. 虚拟环境(一次性)
python -m venv .venv
.venv\Scripts\activate
pip install aiohttp "sqlalchemy[asyncio]" aiomysql aiosqlite cryptography redis `
    pydantic pydantic-settings python-dotenv fastapi "uvicorn[standard]" structlog websockets `
    pytest pytest-asyncio

# 2. 配置
copy .env.example .env   # 填入币安 Key / AI Key

# 3. 运行(本地裸跑, 默认 SQLite 零依赖)
.venv\Scripts\python run.py

# 或 Docker 生产运行(见 docs/docker-deployment.md)
docker compose up -d
```

启动后:
- 面板: http://localhost:8800
- 日志: logs/adaptive.log(JSON)
- 数据: SQLite `adaptive.db`(默认, 28 张表, ORM 自动建表; 生产单机 SQLite + WAL, 见 [database-migration.md](database-migration.md))

## 各运行模式

| 模式 | 命令 | 说明 |
|------|------|------|
| 完整交易(纸面) | `python run.py` | 默认 PAPER_TRADING=true, SOLUSDT |
| 完整交易(实盘) | `.env` 中 `PAPER_TRADING=false` + API Key | 建议先 testnet |
| 前端独立 | `python at90_web\web_serve_standalone.py [--port 9000]` | 只看面板/查库,不跑引擎 |
| 回测 | `python at80_backtest\backtest_run.py --symbol SOLUSDT --days 7` | 收益/胜率/回撤/夏普 |
| Walk-Forward | `from at80_backtest.backtest_walkforward import run_walkforward` | 过拟合检测 |
| 组合回测(真实管线) | `from at80_backtest.backtest_portfolio import run_portfolio_backtest` | 双仓+滑点敏感性(0/10/20bps), 含 win_rate/profit_factor/holding/sortino/calmar/attribution |
| 参数优化(实验) | `from at85_optimizer.optimizer import ParamOptimizer` | 候选生成→回测→落 strategy_versions→排序提案(不自动 activate) |
| 测试 | `.venv\Scripts\python -m pytest tests\ -v` | 1499 个(含覆盖率阈值 fail_under=75%) |
| 测试网只读冒烟 | `$env:RUN_TESTNET_SMOKE="true"; .venv\Scripts\python -m pytest tests\smoke\test_testnet_smoke.py -v -s` | 需真实 testnet.binance.vision; CI 默认排除(`-m "not testnet"`) |
| 测试网真实下单闭环 | `$env:RUN_TESTNET_TRADING="1"; .venv\Scripts\python -m pytest tests\testnet\test_v152_testnet_order_lifecycle.py -v -s` | V11.5 P0-3: 真实下单→成交→账本→对账(opt-in, 属 L3 部署验证) |

> **三种交易模式(纸面 / 测试网真实 / 主网实盘)的配置判据、启动守卫链、运行时状态含义、
> 运维动作速查与模式切换清单**, 见专册 [operating-modes-manual.md](operating-modes-manual.md)。
>
> **V11.6 P1-6**: 测试网**无人值守**运维(soak 配置 / 健康自检 / 财务真相闭环 / 告警处置 /
> 证据收集)见专册 [testnet-runbook.md](testnet-runbook.md)。

## 配置速查(.env)

```ini
SYMBOLS=SOLUSDT              # 逗号分隔多标的
PAPER_TRADING=true           # 纸面模式(模拟成交)
DATABASE_URL=sqlite+aiosqlite:///./adaptive.db  # 默认零依赖; 容器: sqlite+aiosqlite:////app/data/adaptive.db(挂载卷)
REDIS_ENABLED=false          # Stream 事件总线(默认关; 生产可开, 不可用自动降级)
API_HOST=127.0.0.1           # V11.5 P0-1: 默认回环, 不暴露 0.0.0.0; 局域网需改 0.0.0.0 并配 WEB_ADMIN_TOKEN
WEB_ADMIN_TOKEN=             # V11.5 P0-1: Web 写接口共享令牌(空=写接口锁定); 设非空值开启写操作
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

> V11.5 P0-4 起, 迁移手册统一收口到 [database-migration.md](database-migration.md)(协议 / 迁移历史 /
> schema 差异检查)。本节保留速查 `ALTER` 示例, 完整流程见该文档。

ORM `create_all` 只建新表不改旧表; V11.6 P1-4 起前向 ALTER 由迁移框架自动应用(见
[database-migration.md](database-migration.md) §4), 手动 ALTER 仅作历史速查示例:

```sql
-- V2.0: signals 加 indicators(已执行于 2026-09-06)
ALTER TABLE signals ADD COLUMN indicators VARCHAR(2048) NULL;
-- V3.0: signal_result(已执行)
-- 表结构由 ORM create_all 负责(deploy/init.sql 仅建库, 不建表, 见 V11.0 修复)
```

> V11.2 P1-5: 结构变更需同步做三件事 —— (1) 在此登记手动 `ALTER`;(2) 更新
> `at01_common/database.py::SCHEMA_VERSION`;(3) 同步 `tests/unit/test_v129_schema_audit.py`
> 的表清单/关键列锚点(否则测试红)。当前共 28 张表。

> V11.4 P0-7 审计补充(新表完整清单): 新表均由 `create_all` 自动创建、无需手动 ALTER,
> 但历史上有若干新表未逐表登记, 此处补全以便完整还原 schema 演进 ——
> 基表(V1.0): klines / trades / signals / orders / positions / risk_events / ai_advices /
> position_snapshot / strategy_performance; 新增表: signal_result(V3.0) / position_bucket(V4.0) /
> decision_log(V4.0) / ai_parameter_history(V5) / order_intents(V10.1) / order_fills(V10.1) /
> execution_attempts(V10.2)。(其余新表 trade_state / paper_state / trade_records / strategy_versions /
> account_ledger / kill_switch_state / position_lots / sell_allocations / execution_events 已在上方逐条登记。)

> V8.0 新增 `trade_state` / `paper_state` 两张新表, 由 `create_all` 自动创建, 无需手动迁移。

> V9.0 新增 `trade_records` / `strategy_versions` 两张新表(共 17 张), 并给 `position_bucket`
> 追加 `target_ratio` / `target_quantity` / `current_value` 三列 —— 新库自动创建; 存量库需手动
> `ALTER TABLE position_bucket ADD COLUMN ...`。

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
> (`at50_risk/risk_state.py`), 时间窗到期自动恢复、同因续期不重复告警, 每次进入
> PAUSED 落 `risk_events(event_type='risk_state')` 审计。无新表无迁移。

> V10.6 成交后本地记账强一致事务: Position / PositionLot / SellAllocation / AccountLedger
> 四表在单个 DB 事务内提交(任一失败整体回滚, 不留部分镜像), 失败置 `orders.accounting_state
> =RECOVERY_REQUIRED` 并急停冻结。存量库需手动
> `ALTER TABLE orders ADD COLUMN accounting_state VARCHAR(20) NOT NULL DEFAULT 'OK'`(新库自动创建)。

> V10.6 OrderFill 内部幂等键: 原唯一键 (exchange_order_id, exchange_trade_id) 两列皆可空,
> 成交ID缺失时可被重复摄入; 新增非空唯一 `fill_idempotency_key`(格式 `订单ID:成交ID`, 缺失成交ID
> 落 `na`)。存量库需手动回填 + 建唯一索引(新库自动创建):
> ```sql
> ALTER TABLE order_fills ADD COLUMN fill_idempotency_key VARCHAR(128) NOT NULL DEFAULT '';
> UPDATE order_fills SET fill_idempotency_key =
>     COALESCE(exchange_order_id, client_order_id) || ':' || COALESCE(exchange_trade_id, 'na');
> CREATE UNIQUE INDEX ix_order_fill_idem ON order_fills (fill_idempotency_key);
> ```

> V10.6 Signal 与 Execution 数量分离: `signal.quantity` 保持策略原始意图(不再被执行引擎原地改写),
> 实际提交数量(经 REDUCE_ONLY 缩量 / 交易规则过滤调整)独立为 `exec_qty`; `signals.quantity` 落原始意图,
> `orders.quantity` 落实际提交数量。无迁移。

> V10.6 ExchangeInfo 失败禁 BUY(safety 优先): 实盘下单前拉取交易规则(exchangeInfo)失败时,
> BUY 本地拒绝不下单(规则未知下开新仓风险不可控), SELL 减仓仍放行(不新增敞口); 失败不缓存,
> 下次信号自动重试。无迁移。

> V10.6 风险状态机 REDUCE_ONLY 态: 新增「仅减仓」态(禁开新仓、保留卖出), 方向闸门
> `can_buy`(=NORMAL)/ `can_sell`(=NORMAL 或 REDUCE_ONLY); `RiskManager.check()` 按方向分流,
> `_on_signal` / 核心仓 ADD 改用方向闸门。REDUCE_ONLY 不自动恢复(仅 recover/reset 退出)。无迁移。

> V10.7 新增 `execution_events` 订单执行事件日志表(append-only 审计, 共 25 张),
> 由 `create_all` 自动创建, 无需手动迁移。event_id 非空唯一保证事件不重。

> V10.7 订单恢复引擎(`order_recovery.py`)+ 交易所真相对账(`exchange_truth_reconciler.py`):
> 纯 DB/交易所读, 无新表无迁移。UNKNOWN/SUBMITTING 订单周期收敛为交易所真相,
> RECOVERY_REQUIRED BUY 账务重建(进程内补镜像 / 重启后完整记账), SELL 从 DB 开仓 lot
> 确定性重放 FIFO 分配重建(V11.1 P0-4 起, 不再人工冻结)。

> V10.7 风险状态机 RECOVERY_CHECK 态: 急停解除需两步 —— `reset()` 仅 KILLED→RECOVERY_CHECK
> (仍不可交易), 待对账确认一致后再 `confirm_recovered()` 回到 NORMAL, 禁止裸 reset。无迁移。

> V11.0 深度审计修复(F1-F13): 详见 `tasks/cc_task_v11.md`「深度审计修复记录」。要点:
> - F12 `position_lots.client_order_id` 加唯一约束 —— 新库自动创建; 存量库需去重后建唯一索引:
>   ```sql
>   -- 先清除历史重复(保留最早 id 行)
>   DELETE p1 FROM position_lots p1
>     JOIN position_lots p2 ON p1.client_order_id = p2.client_order_id AND p1.id > p2.id;
>   CREATE UNIQUE INDEX ix_position_lot_client_order_id ON position_lots (client_order_id);
>   ```
> - F13 WS 成交流 `@trade` → `@aggTrade`(与 REST `get_agg_trades` ID 口径统一), 无迁移;
>   存量 `trades` 表内 raw/agg 两命名空间混存的历史数据仅影响行情回放, 不影响账本。
> - F10/F11 `get_my_trades_all` 分页 + 恢复重摄取真实手续费; F3/F4/F5/F6 记账原子性/恢复/启动自愈; F8/F9 急停持久化/核心仓闸门 —— 均为代码层修复, 无迁移。

> V11.1 P0-1 Exchange Truth V2: `get_my_trades_all` 现返回 `MyTradesResult`(去重 + 重复/跳号/翻页耗尽
> 完整性检测); 交易所真相对账窗口从本地订单 `created_at` 推导(去硬编码 15min/200 笔); 分页耗尽 →
> `truth_incomplete`/`pagination_exhausted` 只降级(pause 自动恢复)不冻结, `trade_duplicate`/`trade_id_gap`
> 仅可观测性告警。无新表无迁移。对账差异分级处置见 `run.py::_reconcile_loop`。

> V11.1 P0-2 Fee Accounting: 统一 `FeeCalculator`(USDT=quote / SOL=base 折算; 其它资产如 BNB →
> `unpriced` 降级, 不静默 fee=0)。`OrderFill` 新增 `fee_quote` / `fee_valuation_status` 两列,
> 新库自动创建; 存量库需手动:
> ```sql
> ALTER TABLE order_fills ADD COLUMN fee_quote FLOAT DEFAULT 0, ADD COLUMN fee_valuation_status VARCHAR(16) DEFAULT 'zero';
> ```
> 不可计价手续费在摄入路径触发 `risk.pause` 降级(自动恢复), 不冻结。

> V11.1 P0-3 账本重建引擎(`ledger_reconstruction.py`): 从交易所真相(myTrades)重建
> OrderFill / PositionLot / SellAllocation / Position 四张账务表(单事务「先清后插」, 幂等)。
> 用法: `LedgerReconstructionEngine(rest).reconstruct(symbol, cash_before=..., dry_run=False)`。
> 歧义(成交历史不完整 / 缺现金锚 / 超卖 / 成交方向不一致)→ SAFE_MODE 拒绝 apply。
> 现金锚 `cash_before` 是重建窗口起点现金(交易所成交史只有现金变动, 无法还原绝对现金, 必须外部提供)。
> `orders`(client_order_id 本地幂等键)与 `account_ledger`(append-only 审计)不重建。

> V11.1 P0-4 SELL Recovery: RECOVERY_REQUIRED SELL 由 `ExecutionEngine.rebuild_sell_accounting`
> 从 DB 开仓 `PositionLot`(权威未消费态)确定性重放 FIFO 分配, 单事务落 `SellAllocation` +
> 减 lot + 更新 `Position`(平均成本口径)+ `Order`(FILLED + RECOVERED), 完成后重同步内存
> 持仓/FIFO lot 队列。消除「SELL 记账失败 → 永久人工冻结」。无新表无迁移。

> V11.1 P0-5 Reconciliation Matrix: 各对账器差异统一经 `ReconciliationMatrix` 四态判定
> (PASS/DEGRADED/RECOVERY_REQUIRED/KILLED), 由 `run.py::_apply_verdict` 单一决策点处置。
> 规则「单一对账器不得 kill」: 跨源资金级差异(equity_drift/orphan_trade/exchange_only/fill_truth_*)
> 单源即 KILLED; 本地 DB 内部一致性破坏(fill_*/ledger_*/buy_lot/sell_alloc/lot_sum)单一对账器只
> RECOVERY_REQUIRED(自愈不 kill), 需 ≥2 独立对账器同周期佐证才升级 KILLED。无新表无迁移。

> V11.1 P1-5 资金级 Circuit Breaker(`at50_risk/fund_circuit_breaker.py`): Equity/Position/Cash
> 三向漂移分级处置(0.1%/0.2%/0.5%)。Position/Cash 漂移首选 REDUCE_ONLY(减仓去险不冻结, 仅 >0.5%
> 才 PAUSE); Equity 漂移最严重 → 0.1% REDUCE_ONLY、0.2% PAUSE、0.5% KILL。三向独立分级后取最严重
> 一档。纯判定层, 无新表无迁移。

### AI 供应商切换(V9)

AI 顾问通过 `AI_PROVIDER` 选择供应商(`openai/qwen/deepseek`), 默认 `deepseek`,
各供应商的 API Key 与默认 `base_url` 在 `at30_strategy/llm_config.py` 中定义、从 `.env` 读取:

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
.venv\Scripts\python at20_analytics\regime_hmm_train.py --symbol SOLUSDT --days 30 --states 3
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
| POST `/api/breaker/reset` | 手动解除熔断(🔒 需 `X-Admin-Token` 头 = WEB_ADMIN_TOKEN) |
| POST `/api/emergency/kill` | V10: 人工急停(冻结+撤全部未成交单, 持久化, 需 recover 解除)(🔒 需令牌) |
| POST `/api/emergency/recover` | V10: 解除急停(人工恢复交易)(🔒 需令牌) |
| POST `/api/shutdown` | V11.5 P0-1: 请求优雅停机(🔒 需令牌) |
| WS `/ws` | 2s 推送(market/analytics/risk/regime/execution) |

> **V11.5 P0-1 Web 写接口安全**: 所有 POST 写接口统一走 `X-Admin-Token` 头鉴权(共享令牌,
> 不引入 OAuth/JWT)。`WEB_ADMIN_TOKEN` 为空 → 写接口整体锁定(503 fail-closed); 令牌错误 → 401。
> GET 查询接口无需鉴权。默认 `API_HOST=127.0.0.1`(仅本机); 若需局域网访问, 必须同时设
> `API_HOST=0.0.0.0` + 非空 `WEB_ADMIN_TOKEN`, 否则启动 `validate()` fail-fast 拦截。

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

## Pi / Docker 生产部署

> V11.8 起生产运行时统一为「单容器 + SQLite 持久化卷」(替代早期 MySQL/Redis 方案), 详见
> [docker-deployment.md](docker-deployment.md) 与 [raspberry-pi-deployment.md](raspberry-pi-deployment.md)。

```bash
docker compose up -d --build        # 构建并后台启动
docker compose logs -f              # 看日志(JSON)
docker compose restart              # 重启(restart: unless-stopped)
docker compose up -d --build        # 更新代码后重建
# 面板: http://<host>:8800
```

数据/日志/证据持久化在 `./data` / `./logs` / `./evidence`; 容器内 SQLite 已开 WAL/busy_timeout/foreign_keys。

## 停止

Ctrl+C 优雅停机(撤销 WS/关闭 DB/落盘缓冲)。

## 配置保存与回滚(`/admin` 管理页面)

管理页面 `http://<host>:8800/admin` 的「保存配置」**只写文件, 不热生效**。完整闭环:

### 1. 保存

页面上改完点「预览改动」→ 确认 diff 与风险提示 → 「保存配置」。

写盘行为:

- **写前自动备份**为 `<配置文件>.bak.<UTC 时间戳(含微秒)>Z`, 默认保留最近 20 份。
- **只改被改动的键**, 其余行、注释、顺序原样保留。
- 用「同目录临时文件 + `os.replace`」原子替换, 并保留原文件权限位。
- 密钥类字段不可编辑、不回显。

配置文件路径解析顺序: `ADAPTIVE_TRADING_ENV_FILE` → 否则 `./.env`。
Pi 生产上即 `/etc/adaptive-trading/production.env`。

### 2. 重启生效

**方式一：页面内点「🔄 重启服务」**（`/admin` → 管理操作）

- 重启前会**先校验配置文件能否通过启动守卫**，过不了直接**拒绝重启**（避免服务起不来）。
- 走的是与 Ctrl+C / `docker stop` **完全相同**的优雅停机路径。
- 容器内 → `restart: unless-stopped` 自动拉起，约 10~30 秒；页面会自动轮询等它回来。
- **非容器 → 进程退出后不会自动回来**，页面会明确提示需手动启动。

**方式二：命令行**

```bash
# Pi / Docker 生产
docker compose --env-file /etc/adaptive-trading/production.env up -d

# 本地裸跑
# Ctrl+C 后重新 python run.py
```

> ⚠️ **风险提示**：配置文件过了 `validate()` 也可能在运行期启动失败（例如行情或数据库连不上）。
> 此时服务起不来、页面也打不开，需要 SSH 到宿主机用 `.bak` 备份恢复后再重启。

重启后用 `/ops` 页面确认三态, 或:

```bash
curl -fsS http://127.0.0.1:8800/api/operator-status
docker compose --env-file /etc/adaptive-trading/production.env config   # 确认配置可渲染
```

### 3. 回滚

页面「恢复上一份配置」, 或手动:

```bash
ls -t /etc/adaptive-trading/production.env.bak.* | head -1     # 最近一份
cp /etc/adaptive-trading/production.env.bak.<时间戳> /etc/adaptive-trading/production.env
docker compose --env-file /etc/adaptive-trading/production.env up -d
```

> 回滚前会**先把当前文件也备份一份**(可再次回退), 且恢复源已在备份前读入内存 ——
> 「保存后立刻回滚」不会毁掉要恢复的那份备份。

### 4. 保存失败时

| 现象 | 原因与处置 |
|------|-----------|
| 「当前不可写」 | 容器未挂载配置目录, 或权限不足 → 见下方「让容器可写」; 或在宿主机直接改文件 |
| 「校验未通过」 | 逐项看列出的问题; 主网相关的会说明缺哪个确认字段 |
| 主网真实模式被拒 | 守卫拦截(设计如此), **不会被绕过**; 补齐 `LIVE_TRADING_CONFIRM` 与 `MAINNET_API_SCOPE_CONFIRM` |
| 「令牌无效 / 未填写」 | 右上角令牌徽章会直接显示状态; 令牌值在服务器上 `grep WEB_ADMIN_TOKEN <配置文件>` 查看(页面不显示它) |
| 写接口返回 503 | 服务端未配置 `WEB_ADMIN_TOKEN` → 写操作被 fail-closed 锁定, 配好令牌后重启 |

### 4.5 关闭写接口鉴权(个人局域网, 可选)

V12.6 起**默认 `WEB_ADMIN_AUTH=off`** —— 写接口无需令牌(操作者要求方便优先)。
要恢复 fail-closed, 设 `on` + 非空令牌; 或如下显式关闭:

```ini
WEB_ADMIN_AUTH=off
```

关闭后:

- 页面**不再需要令牌**, 右上角徽章显示「鉴权已关闭」;
- `validate()` 不再因「非回环 `API_HOST` + 空 `WEB_ADMIN_TOKEN`」拒绝启动;
- 启动日志打印醒目告警, `/admin` 与 `/` 面板常驻橙色横幅, `/ops` 记为 **WARN**。

> ⚠️ **代价**: 局域网内任何设备(手机、电视、IoT)无需任何凭据即可调用写接口 ——
> 包括**改配置**(可关掉启动对账、抬高高回撤阈值等安全网)、**恢复急停**(解除因对账漂移
> 触发的冻结)、**停机**。它**不能**单凭这一点打开主网真实交易(那还需要主网 key +
> `LIVE_TRADING_CONFIRM` + `MAINNET_API_SCOPE_CONFIRM` + 通过就绪自检)。
>
> 该开关本身也可以在 `/admin` 页面上改(运维类), 改完需重启生效。

### 5. 让容器可写配置(Pi)

`docker-compose.yml` 已支持把外置配置目录挂进容器:

```ini
# /etc/adaptive-trading/production.env
HOST_CONFIG_DIR=/etc/adaptive-trading
```

并让容器用户(uid 999)可写该目录与文件:

```bash
chown -R 999:999 /etc/adaptive-trading
chmod 700 /etc/adaptive-trading
chmod 600 /etc/adaptive-trading/production.env
```

> **权衡**: 这会让容器进程可写该文件(它本来就能读到里面的密钥 —— env 已注入)。
> 文件仍**不是** world-readable, 但不再是 `root:root`。不挂载时管理页面仍可读配置与预览改动,
> 只是「保存」会明确报不可写并提示宿主机操作。
