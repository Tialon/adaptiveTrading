# CLAUDE.md — 仓库导览

> 给两种读者：**接手代码的人**，和**在这个仓库里干活的 AI**。
> 目标是一页之内知道「这是什么 / 红线在哪 / 从哪读起 / 怎么跑」。

## 这是什么

**Binance 现货 SOLUSDT 自动摆动交易系统**，无人值守，资金量级约 2 万 RMB。
Python 3.13 + asyncio **单进程**（非微服务），回测与实盘跑**同一份策略代码**。

当前状态：**纸面（paper）为默认**，Docker 生产运行时已就绪，Pi arm64 曾部署（2026-09-11，见 `docs/verification/pi-deployment.md`；
当前 Level 3 状态仍为 PI_DEPLOY / PI_ARM64_SMOKE **NOT_EXECUTED**），
**主网实盘需人工 go/no-go，默认被守卫拦截**。

---

## ⛔ 冻结红线（改任何代码前先读这一节）

产品定义已冻结，以下**不可新增、不可放宽**：

| 冻结项 | 说明 |
|--------|------|
| **单交易所** | 只用 Binance，不接第二家 |
| **单币种** | `SYMBOLS=SOLUSDT`，非 SOLUSDT 会被 `settings.validate()` **拒绝启动** |
| **Spot 现货，非合约** | 走 `/api/v3/*`（不是 `/fapi`）。⚠️ **现货没有 `reduceOnly` 参数**，「卖出不得超持仓」必须**客户端实现** |
| **双仓 + 低频** | 核心/交易/现金三桶；不做高频 |
| **AI 只建议不下单** | 优化器只产 proposal（`active=False`），`activate` 需人工显式调用 |
| **禁止** | 新增交易策略 / 币种 / 合约 / 高频 / LLM 下单 |

**绝对禁止主网误执行**。三道闸门不得放宽：

1. `Settings.mainnet_blocked_reason()` — **真钱交易主网**（`PAPER_TRADING=false` + `BINANCE_TESTNET=false`）
   且未显式 `LIVE_TRADING_CONFIRM=true` → 启动拦截（V12.6：纸面 + 主网行情 = 主网观察，放行）
2. `at01_common/mainnet_readiness.py` — **真钱交易主网**时启动前**九项**自检，`MAINNET_API_SCOPE_CONFIRMED` 默认 false
   （V12.6：触发条件是「非纸面 + 连主网」，不是「是否连主网」—— 主网观察模式无真钱能力，放行）
3. `at01_common/testnet_gate.py` — 测试网真实执行需 `RUN_TESTNET_TRADING=1` + key 齐备

**安全契约**（改动时不得破坏）：
- **唯一咽喉**：`ExecutionEngine.execute()` 是全系统唯一下单点，仅 `run.py` 两处经闸门调用。无 bypass、无反射动态下单、**Web 无下单端点**。
- **单一权威**：交易许可只由 `at50_risk/trading_gate.py::TradingGate` 判定。`health.can_buy/can_sell` 直接取自它，**绝不虚报「可买」**。
- **实盘财务真相**锚定交易所对账链（`LIVE_ACCOUNT_LEDGER_MODE = EXCHANGE_TRUTH_RECONCILIATION`），不在实盘补写 `account_ledger`。
- **自动恢复不放宽任何判定**（V13）：解除冻结**唯一**走 `at50_risk/recovery_flow.py`（人工与自动共用），
  且**仅限可自愈来源**——`MANUAL` / `AUTO_EQUITY` / `AUTO_ACCOUNTING` / `AUTO_RECONCILE` / 未知来源
  **永远要人**。判据是「能否自证」：账本与真实资产对不上时，系统无法自证自己没算错。
- **解冻 ≠ 允许下单**：恢复流程只解冻，能否成交仍由 `TradingGate` 逐笔判定。
- **AI 只建议不下单**（不变）：AI Review 包只读，优化器只产 `active=False` 的 proposal。

---

## 从哪读起

**编号 = 阅读顺序 = 数据流顺序**（十位是层号，个位 `0`=主 / `5`=同层辅助）。

```
L0  at01_common      基础(横切)  settings / models / database / logger / migrations / 监督
L1  at10_market      行情接入    WS 重连 / 状态预热 / 数据校验
L2  at20_analytics   分析        VWAP / CVD / Whale / 吸筹 / regime / alpha
L3  at30_strategy    策略        Entry 评分 / Exit / 多策略加权融合决策
L4  at40_portfolio   组合        三桶(核心/交易/现金) / 成本曲线
L5  at50_risk        风控        TradingGate(六维+两维) / 生命周期 10 态 / 资金熔断
                                recovery_flow(解冻唯一实现) / auto_recovery(仅自动来源)
L6  at60_execution   执行        幂等 / 交易状态机 / 下单(唯一咽喉) / 对账 / 账本
L7  at70_journal     记录        日报 / HODL 对标 / AI 复盘包(ai_review.py)
L8  at80_backtest    研究        回测 / Walk-Forward        (离线)
L8  at85_optimizer   研究        参数优化                    (离线)
L9  at90_web         展示(横切)  REST / WS / 面板 / 管理控制台 / 系统健康 / 无人值守向导
    run.py           编排层      AdaptiveTradingSystem —— 唯一编排器
```

**推荐路线**：

| 想搞懂 | 按顺序读 |
|--------|----------|
| 整体 | [`docs/architecture.md`](docs/architecture.md) §1 分层图 → §2 时序图 → §3 **四套状态机** |
| 一次成交怎么发生 | `architecture.md` §2.1 → `run.py::_on_trade` → `_on_signal` → `at60_execution/execution_executor.py` |
| 为什么不能买 | `at50_risk/trading_gate.py::can_open_position()`（八项逐条判定） |
| 交易逻辑本身 | [`docs/trading-logic.md`](docs/trading-logic.md) |
| 用户看到什么 / 无人值守边界 | [`docs/product/operator-experience.md`](docs/product/operator-experience.md)、[`docs/product/unattended-operation.md`](docs/product/unattended-operation.md) |
| 每个文件干什么 | [`docs/module-map.md`](docs/module-map.md) |

> ⚠️ **四套状态机是最大的困惑源**，务必先读 `architecture.md` §3：
> `SystemLifecycle`(10 态，管全系统) / `RiskStateMachine`(4+1，管风控) /
> `TradeState`(5 态，管单标的这一轮) / 运行时分类器(7 态，**派生**，只用于显示)。

---

## 常用命令

```bash
# 跑起来(纸面, 默认)
python run.py                       # → http://127.0.0.1:8800

# 测试(与 CI 同款)
uv run pytest -q --cov --cov-report=term-missing --cov-fail-under=75 -m "not testnet"

# 静态检查
uv run ruff check .
uv run mypy

# 回测
python at80_backtest/backtest_run.py --symbol SOLUSDT --days 7

# 文档里的 Mermaid 图语法校验(需 playwright, 见脚本 docstring)
python scripts/check_docs_mermaid.py docs/

# Docker
docker compose up -d && docker compose logs -f
```

### 三档环境（V14 起口径统一，别再漂移）

| 档 | 环境 | 数据库 | Redis | 启动 |
|:--:|------|--------|-------|------|
| **Level 1** | Windows + Python | **SQLite** | 不需要 | `.\scripts\start-local.ps1` |
| **Level 2** | Windows + Docker | **MySQL 8** | **Redis 7** | `docker compose up -d` |
| **Level 3** | Pi + Docker | **MySQL 8** | **Redis 7** | 同 Level 2 |

依赖等级（代码实证，见 `docs/architecture.md` §6.5）：
**MySQL = REQUIRED**（唯一持久化）/ **Redis = OPTIONAL**（只有发布方、无消费方的事件流旁路）。
Redis 不可用时应用照常跑，但会**显式记为降级**（健康报告 + 事件流），不静默。

### 本机启动（Level 1）

`.env` 里 `DATABASE_URL` 指向 MySQL → 直接 `python run.py` 会在 `init_db()` 处挂掉。
**一条命令解决，不必手工设环境变量**：

```powershell
.\scripts\start-local.ps1        # SQLite + Redis off + 自动开浏览器
```

它**不改 `.env`**（含生产库密码），只在进程环境里覆盖。

> 🔴 脚本会先做**实盘安全预检**：运行参数优先级是 **DB > env**，老库里的 `runtime_config`
> 覆盖会盖过脚本设的环境变量。目标库若含实盘覆盖，脚本**拒绝启动(exit 2)** ——
> 实测踩到过：`adaptive.db` 里的历史残留把「本机开发启动」直接带到了主网。

另：**写接口鉴权自 V12.6 起默认关闭**（`WEB_ADMIN_AUTH=off`，操作者要求方便优先）——
页面直接可写，无需令牌。代价是**任何能访问该端口的人都能改配置 / 恢复急停 / 停机**；
启动日志、页面、`/ops` 会常驻告警。

> ⚠️ 本机 `API_HOST=127.0.0.1` 只有本机可连，故这个默认在本机风险可控。
> **Pi 上是 `API_HOST=0.0.0.0`（整个局域网可达）** —— 那里是否也用 `off` 是操作者的
> 明确选择，不是默认行为的推论。
>
> 要恢复 fail-closed：设 `WEB_ADMIN_AUTH=on` + 非空 `WEB_ADMIN_TOKEN`。

---

## 提交约定

- **只有 `main` 分支**，不建其他分支。
- **每完成一个工作单元即 commit 并 push**，不要攒多个任务一次提交。
- 提交前跑 `ruff` + `pytest -m "not testnet"`，两者必须绿。
- **改实现，不改断言**：测试挂了先怀疑实现。若确实要改断言，必须在提交信息里说明理由。
- 不提交 `.env` / 密钥 / `*.db` / `logs/` / `evidence/` / `reports/`（`.gitignore` 已覆盖）。

---

## 目录地图

```
adaptiveTrading/
├── run.py                  唯一编排器(1060 行)
├── at01_common … at90_web  11 个代码包, 编号即阅读顺序
├── Dockerfile              ⚠️ 必须留根目录(compose 卷用相对路径, 见 deploy/README.md)
├── docker-compose.yml      ⚠️ 同上
├── deploy/                 部署资产: init.sql(建库) / pi/production.env.example
├── docs/                   工程文档, 索引见 docs/README.md
│   └── tasks/              历史工单归档(18 份, ⚠️ 里面是旧包名)
├── design/                 ⚠️ V1.0 时期设计图存档, 已与代码脱节
├── migrations/             前向迁移 SQL
├── scripts/                db_backup.py / check_docs_mermaid.py
├── tests/                  2500 条(not testnet)(unit / integration / long_running / smoke / testnet)
└── data/ logs/ evidence/ reports/   运行时产物, 已 gitignore
```

**权威来源**（文档与代码冲突时以代码为准）：

| 事实 | 权威位置 |
|------|----------|
| 数据表结构 | `at01_common/models.py`（29 张表） |
| 配置项与校验 | `at01_common/settings.py` + `validate()` |
| 数据库版本号 | `at01_common/database.py::SCHEMA_VERSION` |
| 交易许可 | `at50_risk/trading_gate.py` |
| 人话状态与通知分级 | `at01_common/operator_narrative.py` |
| 解除冻结 | `at50_risk/recovery_flow.py`（人工与自动共用） |
| 启动守卫顺序 | `at01_common/wiring.py::wire_system()` |

---

## 诚实原则（本项目的一贯要求）

**不虚报**。具体地：

- 区分「**代码已实现**」与「**真实环境已执行**」——七态模型
  IMPLEMENTED / READY_TO_RUN / EXECUTED / PASSED / FAILED / BLOCKED / NOT_EXECUTED。
- 没跑过的就说没跑过（如 7h/24h soak 属真实挂机项）。就绪等级目前 **L2，不报 L3**。
- 覆盖率达标不等于功能正确；「无报错」不等于「通过」。
- 守卫拦截要如实标注原因，**不为了让页面好看而放宽判定或隐藏 `can_buy=false`**。
