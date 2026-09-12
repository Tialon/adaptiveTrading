# Level 1 — 本机直接运行验证

> 三级验证的第一级。**只有 Level 1 + Level 2 达到高完成度，才允许进入 Level 3（Pi）。**
> 状态词严格区分「代码写完」「配置已存」「服务已跑」「实际验证过」。

## 怎么跑

```bash
# 本机启动(无 MySQL/Redis, 用 SQLite 覆盖)
DATABASE_URL='sqlite+aiosqlite:///./adaptive.db' REDIS_ENABLED=false python run.py
# → http://127.0.0.1:8800
```

## 验证矩阵

| 项 | 命令 / 端点 | 状态 |
|----|------------|------|
| 静态检查 | `ruff check .` / `mypy` | **PASSED** |
| 单元 + 集成测试 | `pytest -q -m "not testnet"` | **PASSED**（V13 时点 2500） |
| HTTP `/api/health` | `curl :8800/api/health` | **PASSED** |
| HTTP `/api/trading-mode` | 返回三模式 + 可启动性 | **PASSED** |
| HTTP `/api/trading-mode/preview` | 不写盘, 返回 diff + 守卫预检 | **PASSED** |
| HTTP `/api/trading-mode/apply` | 只保存 + 提示重启 | **PASSED** |
| HTTP `/api/operator-status` | 三模式字段 + 买卖许可 | **PASSED** |
| 模式切换跨重启 | `tests/integration/test_v129_mode_switch_reload.py` | **PASSED**（7 条） |
| SQLite `runtime_config` 持久化 | 写入后重启仍在 | **PASSED** |
| 交易安全闭环（纸面） | BUY/SELL/重复信号/幂等/UNKNOWN/急停/对账漂移 | **PASSED**（既有单测） |
| 真实测试网 | `pytest -m testnet` | **PASSED**（V13 实测, 见文末——本环境确有测试网密钥） |
| 本机 MySQL 迁移 | `init_db` 对存量库补列 | **PASSED**（V13 实测，见下） |

## 本机已修复的真实故障（V12.9）

**现象**：页面切换模式后，**重启服务起不来**（不是切换报错，是切换后重启即死）：

```
RuntimeError: 运行模式解析失败: 配置冲突:
  paper_trading 显式设为 True, 但 TRADING_MODE=testnet 要求 False
```

**成因**：`.env` 里遗留 `PAPER_TRADING=true`（迁移前的显式值），DB 里有
`TRADING_MODE=testnet`（页面切换写入）。DB 覆盖生效后 `trading_mode=testnet`，
但**冲突检查**仍把那条**已被取代的** `.env` 值当成操作者意图 → 判冲突 → 拒绝启动。

**修复**：`TRADING_MODE` 被 DB 覆盖时，它**推导出的**字段一并从冲突判定中剔除。
**没有放宽冲突检测** —— 同层（都在 `.env`）矛盾时照常 fail-closed，有测试锚定。

**回归**：`tests/integration/test_v129_mode_switch_reload.py`（7 条），走
「env 造 Settings → 叠加 DB 覆盖 → 排除被覆盖字段 → resolve → 断言」全链路，
不只测单函数。

## 本机与 Docker 的分工

Level 1 验证**代码与配置语义**（模式解析、切换、持久化、守卫链）。
Level 2 验证**运行时载体**（镜像、卷、重启、健康检查）。
两者都要过，才谈 Pi。


## V13 实测补充（2026-09-12）

### 本机环境：直接用 Docker 里的 MySQL / Redis

本机 Docker 已跑 **MySQL 8.0.46**（:3306，库 `adaptive_trading`）与 **Redis 7**（:6379），
`.env` 的 `DATABASE_URL` 直接可用 —— **不再需要 SQLite 覆盖**，Level 1 跑在与生产同款的
MySQL 上。

```powershell
.venv\Scripts\python.exe run.py      # → http://127.0.0.1:8800
```

### 本轮实测暴露并修复的一个真 bug：存量库静默缺列

首次真实启动后，第一笔纸面下单即报：

```
pymysql.err.OperationalError: (1054, "Unknown column 'reduce_only' in 'field list'")
```

用仓库自带的漂移检查工具定位：

```bash
.venv/Scripts/python.exe -m at01_common.schema_check
# → 缺列 account_ledger.{commission,commission_asset,matched_cost,realized_pnl}
#        orders.{accounting_state,reduce_only}   共 6 处
```

**根因**: V9.0 / V10.3 / V10.5 / V10.6 的增量列当年只登记在文档里，执行方式写的是
「存量库手动 ALTER」—— **从来没有可执行的迁移** `create_all` 又只建缺失的**表**、
不给既有表加列，于是任何建表早于该版本的库都会静默缺列。

**修复**: 把这 6 列补进 `migrations.py::_ADDITIVE_COLUMNS`，由 `_ensure_additive_columns()`
幂等补齐（先查 `PRAGMA` / `information_schema` 再 ALTER）。修复后
`schema_check` 报「无漂移」；新增 `tests/unit/test_v13_additive_columns.py`(7 条)锚定，
含「存量表已有数据时补列必须拿到 DEFAULT」。

### 本机冒烟

```bash
curl :8800/api/operator-status   # 首屏结论卡
curl :8800/api/setup/status      # 无人值守五要素
curl :8800/api/operator-log      # 今天发生了什么
curl :8800/api/ai-review/latest  # AI 复盘包
```

---

## V13 实测：真实 Binance 测试网（2026-09-12）

> 上一版中「真实测试网」为 `NOT_EXECUTED`。本环境**确有测试网密钥**（`.env` 中
> `BINANCE_TESTNET_API_KEY/SECRET` 为 64 位真实值），且 `testnet.binance.vision` 可达
> （`/api/v3/ping` → HTTP 200），因此**执行了真实测试网**。

### 结果

| 项 | 命令 | 结果 |
|----|------|------|
| 只读冒烟 | `RUN_TESTNET_SMOKE=true pytest tests/smoke/test_testnet_smoke.py` | **PASSED**（4 条） |
| **真实订单生命周期** | `RUN_TESTNET_TRADING=1 pytest tests/testnet/test_v152_testnet_order_lifecycle.py` | **PASSED**（2 条） |

`test_v152` 覆盖的链路（日志实测）：

```
行情时间同步(base_url=https://testnet.binance.vision)
  → ① 无成交生命周期: 远低于市价限价买单 → create_order → get_order(NEW)
                      → cancel_order → get_order(CANCELED) → myTrades(空)
                      → 交易所 SOL 余额不变
  → ② 真实成交闭环: MARKET 买入(avg_price=102.24, qty=0.073)
                    → 本地记账(Position / PositionLot / Order / OrderFill)
                    → MARKET 卖出 → SELL 分配(SellAllocation) → 交叉对账
```

覆盖到的任务书要求项: **account sync / market data / order / fill / accounting /
reconciliation**。

### 未覆盖（如实标注）

- **「signal → risk → order」全系统路径未在测试网模式跑通** —— 上表是**直接驱动
  ExecutionEngine** 的订单生命周期验证，不是「让策略自己发信号」。原因见下。
- **restart 未在测试网模式下单独复验**（容器重启已在 Level 2 覆盖）。

### 为什么没有跑「全系统测试网」——一个真实的 fail-closed 现场

把容器切到测试网模式并重启后，系统**立即自己冻结了**：

```
KILL | 系统已自动停止交易(账户与账本对不上)
     | origin=AUTO_RECONCILE
     | reasons=['position:exchange_only SOLUSDT', 'exchange_truth:orphan_trade ...']
```

**这是正确行为**：测试网账户里有上一步订单生命周期测试留下的持仓/成交，
本地账本自然对不上 —— 系统拒绝在一个无法解释的账户状态下交易。

随后用它验证了 V13 的恢复链路修复（这正是 Pi 上卡住的那个问题）：

```bash
POST /api/emergency/recover
→ steps: ['已解除急停冻结', '风险态: PAUSED → 正常',
           '生命周期: 安全模式 → 就绪', '生命周期: 就绪 → 交易中']
→ precondition.missing: ['对账尚未通过']
→ note: 已解除冻结。能否实际下单仍由交易闸门逐笔判定。
         注意: 对账尚未通过 —— 在这些项恢复前, 闸门仍会拒绝开仓。
```

恢复后实测 `can_buy=False`, 阻断原因 `对账未通过` —— 印证了设计原则
**「解冻 ≠ 允许下单」**。修复前，这个状态**只能靠重启进程脱身**。

验证完成后容器已切回模拟模式并停止。

---

## V14 实测：Level 1 功能矩阵（Windows + SQLite，2026-09-12）

> 任务书 §5：「CC **必须实际执行**，而不是只看 pytest」。
> 本节的每一项都由 `scripts/functional_matrix.py` 对一个**真实运行**的实例打点，
> 结果落盘在 `logs/functional_matrix.json`，可随时复跑。

运行方式（一条命令，不需要手工设环境变量）：

```powershell
.\scripts\start-local.ps1              # 默认 data\local-dev.db, Redis 关闭
python scripts/functional_matrix.py --base http://127.0.0.1:8801
```

### 结果：**29/29 PASSED**

| 分区 | 覆盖 | 结果 |
|------|------|------|
| §5.1 启动 | 程序启动 / 进程 running / schema 初始化 + migration / SQLite 自动创建 / 运行时健康快照可读 | **PASSED**（5/5） |
| §5.2 页面 | `/` `/setup` `/admin` `/ops` 结构完整；首屏能判断；无需操作；模式；健康；事件流；AI Review；急停按钮 | **PASSED**（11/11） |
| §5.3 配置 | 读取 → 修改 → 保存 → **DB 持久化** → 自动重载路径 | **PASSED**（4/4） |
| §5.4 模式 | 三模式齐备 / 各自预检给确定结论 / **非法模式 fail-closed** | **PASSED**（5/5） |
| §5.5 风控 | 正常 → 急停阻断 → 恢复逐层解开 → **解冻后仍由 TradingGate 判定** | **PASSED**（4/4） |

关键证据：

```text
首屏        : 系统运行正常 / "模拟运行中。系统运行正常, 无需操作。"
降级时      : "系统正常, 无需操作(有 1 项降级: 事件总线(Redis))。"
非法模式    : {"ok": false, "error": "未知模式: 'not_a_mode'; 只接受 paper/testnet/live"}
急停        : status=KILLED can_buy=False
恢复        : steps=['已解除急停冻结', '生命周期: 安全模式 → 就绪', '生命周期: 就绪 → 交易中']
解冻≠可交易 : 恢复后仍由 TradingGate 逐笔判定(契约未破坏)
配置持久化  : 库里 RISK_MAX_DAILY_LOSS=0.031(apply 不热生效, 需重启后运行值才变)
```

### `scripts/start-local.ps1` 的安全预检（实测踩出来的）

```powershell
.\scripts\start-local.ps1 -DbFile adaptive.db
# → exit 2:
#   该库会让本机开发进入【实盘主网】: TRADING_MODE=live,LIVE_TRADING_CONFIRM=true,...
#     1) 用默认的干净开发库:  直接跑 start-local.ps1(不带 -DbFile)
#     2) 换一个库:            -DbFile data\other.db
#     3) 继续用这个库:        先在 /admin 页面把模式切回「模拟」
```

原因：运行参数优先级是 **DB > env**，一个老库里的 `runtime_config` 覆盖会**盖过**
脚本设的环境变量。实测 `adaptive.db` 里残留着早先模式切换测试写入的实盘覆盖，
`start-local.ps1` 一跑系统直奔主网（靠 `live_equity` 播种失败才 fail-closed）。

因此默认库改为 `data\local-dev.db`（Level 1 状态可丢弃），并在启动前预检。
