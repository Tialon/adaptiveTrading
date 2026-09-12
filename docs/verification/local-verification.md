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
| 真实测试网 | `pytest -m testnet` | **NOT_EXECUTED**（无密钥） |
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
