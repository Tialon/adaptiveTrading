# Level 2 — 本机 Docker 完整服务验证

> 三级验证的第二级。**Level 1 + Level 2 都达标才允许进入 Level 3（Pi）。**
> 本文件记录的是**实际执行过**的命令与结果（2026-09-12）。

## 前置：本环境 `docker.io` 不可达

本机无法访问 `registry-1.docker.io`（EOF）。必须用 Dockerfile 预留的镜像站 ARG：

```bash
docker build \
  --build-arg PYTHON_BASE=docker.1ms.run/library/python:3.13-slim \
  --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  --build-arg UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple \
  --build-arg GIT_SHA=$(git rev-parse HEAD) \
  -t adaptive-trading:v129-local .
```

## 验证矩阵（实测）

| 项 | 命令 | 结果 |
|----|------|------|
| Build | 上面的 `docker build` | **PASSED** — `adaptive-trading:v129-local` 301MB |
| Start | `docker run -d -p 8888:8800 …` | **PASSED** — 容器 `Up` |
| Health | `curl :8888/api/health` | **PASSED** — `{"status":"ok","running":true}` |
| Mode | `curl :8888/api/trading-mode` | **PASSED** — `paper (模拟) source=TRADING_MODE` |
| Apply → DB | `POST /api/trading-mode/apply` + `/api/admin/config/apply` | **PASSED** — 入 `runtime_config` |
| Restart | `docker restart at-v129` | **PASSED** — 重启后仍 `ok` |
| **DB 持久化** | 重启后查 `/app/data/adaptive.db` | **PASSED** — `TRADING_MODE=paper` / `PAPER_TRADING=true` / `RISK_MAX_DAILY_LOSS=0.02`，history 3 条 |
| testnet 模式切换 | `apply {"mode":"testnet"}` | **BLOCKED（正确）** — 容器无测试网密钥 → 守卫 fail-closed |
| compose 全流程 | `docker compose up/down` | **PASSED（V13 实测，见 §V13）** |
| 真实 testnet 下单 | — | **NOT_EXECUTED** |

> **`BLOCKED` 是正确行为，不是失败**：容器没配测试网 key，切 testnet 被守卫拒绝。
> 这正是 fail-closed 该有的样子。

## 本次构建暴露并修复的一个真 bug（V12.6 埋下）

```
failed to calculate checksum ... "/L9": not found
```

**根因**：Dockerfile 的 `COPY` 行尾写了注释：

```dockerfile
COPY --chown=app:app at90_web/       ./at90_web/        # L9 展示(横切)
```

**Dockerfile 的 `COPY` 不支持行尾注释** —— `#` 之后被当成**额外的源路径**。

**为什么一直没发现**：V12.6 改了这段格式后，**从未真正跑过 `docker build`**
（CI 的 docker-smoke 只在 GitHub Actions 跑，本地没验证）。
**只有真的执行 Docker 构建才会暴露** —— 这正是 Level 2 存在的意义。

**修复**：注释移到独立行，并留警示（`Dockerfile:69-71`）。

## 容器配置与 compose 的差异（实测提醒）

裸 `docker run` 时我漏了 `DATABASE_URL`，容器用了默认 `./adaptive.db`（非 `/app/data/`），
导致第一次持久化验证查不到库。**compose 里是显式设了的**：

```yaml
DATABASE_URL: "sqlite+aiosqlite:////app/data/adaptive.db"   # 4 斜杠 = 绝对路径
```

**验证持久化时必须带这一条**，否则测的不是 compose 的真实行为。

---

## V13 实测：compose 全流程（2026-09-12）

> 上一版中本项为 `NOT_EXECUTED`。V13 补齐，**全部为实际执行结果**。

| 项 | 命令 | 结果 |
|----|------|------|
| Build | `docker compose build` | **PASSED** — `adaptive-trading:v13-local` |
| Up | `docker compose up -d` | **PASSED** — `Up (healthy)`，端口 `0.0.0.0:8800` |
| Health | `curl :8800/api/health` | **PASSED** — `{"status":"ok","running":true}` |
| 三模式 | `curl :8800/api/trading-mode` | **PASSED** — `paper`，标签 `模拟 / 测试 / 实盘` |
| **无人值守向导** | `curl :8800/api/setup/status` | **PASSED** — `ready=true`「系统已进入无人值守运行」 |
| **操作员事件流** | `curl :8800/api/operator-log` | **PASSED** — 真实成交链路：`发现信号 / 风控通过 / 订单已提交 / 已成交 / 交易完成` |
| Config → DB | `POST /api/admin/config/apply` | **PASSED** — `RISK_MAX_DAILY_LOSS` 入 `runtime_config` |
| Restart | `docker compose restart` | **PASSED** — 重启后仍 `ok` |
| **DB 持久化** | 重启后查 `/app/data/adaptive.db` | **PASSED** — 配置值存活 |
| **V13 迁移** | 同上 | **PASSED** — `schema_version` = `001`,`002`；29 张领域表 + `schema_version` |
| **Down → Up** | `docker compose down && up -d` | **PASSED** — 容器移除重建后配置与事件流仍在（事件 24 → 39 条） |
| **配置生效进度** | `curl :8800/api/admin/reload-status` | **PASSED** — `ready=true`「系统已恢复无人值守。」六项全绿 |
| 真实 testnet 下单 | — | **NOT_EXECUTED**（无密钥） |

### 本轮实测暴露并修复的一个真 bug

首次跑 compose 时 `/api/admin/reload-status` 一直显示「行情连接: 等待连接」——
**明明已经连上了**。

根因: 该接口把「本次启动」的事件窗口锚定在**最近一条 `STARTUP` 之后**, 但
`KIND_CONNECT` 是在 `wiring` 阶段发出的, **比 `STARTUP` 早** —— 于是本次启动的 CONNECT
被排除在窗口外。改为以**上一次 `STARTUP`** 为下界(即「本次启动的全部事件」),
重新构建镜像后实测 `ready=true`。

这条只有**真的把容器跑起来**才会暴露: 单元测试与代码走读都不会发现窗口边界错了。

### 容器内唯一的报错(如实记录)

```
Redis 不可用: Connect call failed ('127.0.0.1', 6379)
```

**预期行为**: Redis 是可选依赖(默认关), compose 未内置 Redis, `.env` 的
`redis://localhost:6379` 在容器里指向自身。系统按设计**静默降级**, 不影响交易链路。

---

## V14 实测：Docker 正式切 MySQL + Redis（2026-09-12）

> 上一版 Docker 跑的是 **SQLite**，Pi 计划跑 MySQL —— 属于架构漂移（V14 §7 明确禁止）。
> 本版把 Docker(Level 2) 与 Pi(Level 3) 统一为 **MySQL + Redis**，只有 Level 1 本机开发用 SQLite。

### 环境模型（三处口径统一）

```text
Level 1   Windows + Python + SQLite              无 MySQL / 无 Redis
Level 2   Windows + Docker + MySQL 8 + Redis 7   ← 本节
Level 3   Pi      + Docker + MySQL 8 + Redis 7   同 Level 2, 仅宿主机目录/密钥/资源不同
```

### 依赖等级（代码实证，非照抄文档）

| 依赖 | 等级 | 依据 | 不可用时 |
|------|:----:|------|----------|
| **MySQL 8** | **REQUIRED** | 唯一持久化 | 启动 fail-closed；运行期写库失败 → 订单中止（不提交交易所） |
| **Redis 7** | **OPTIONAL** | 只承载一条**有发布方、无消费方**的事件流旁路 | 应用照常运行，但**显式记为降级** |

详见 [`../architecture.md`](../architecture.md) §6.5。

### 生命周期矩阵（全部实际执行）

| 项 | 命令 | 结果 |
|----|------|------|
| Build | `docker compose build` | **PASSED** — `adaptive-trading:v14-local` |
| Up | `docker compose up -d` | **PASSED** — 三容器全 `healthy` |
| **健康等待** | 启动顺序 | **PASSED** — `Waiting → Healthy → Starting`（`condition: service_healthy` 生效） |
| Health | `curl :8800/api/health` | **PASSED** — `{"status":"ok","running":true}` |
| **MySQL 后端** | `docker exec printenv DATABASE_URL` | **PASSED** — `mysql+aiomysql://adaptive:***@mysql:3306/adaptive_trading` |
| **MySQL 表** | `information_schema` | **PASSED** — 30 张（29 领域表 + `schema_version`） |
| **migration** | `schema_version` | **PASSED** — `001`, `002` |
| **Redis** | `redis-cli ping` / 应用事件 | **PASSED** — `PONG`；事件流「Redis 已连接(事件总线可用)」 |
| Restart 应用 | `docker compose restart adaptive-trading` | **PASSED** — 配置值存活 |
| Restart MySQL | `docker restart adaptive-trading-mysql` | **PASSED** — 应用自行恢复 |
| Restart Redis | `docker restart adaptive-trading-redis` | **PASSED** — 应用自行恢复 |
| **down → up** | `docker compose down && up -d` | **PASSED** — 配置 `0.028` 存活；事件 115 → 149；migration 记录完整 |
| 命名卷 | `docker volume ls` | **PASSED** — `adaptive_mysql_data` / `adaptive_redis_data` |

### §9 Redis 闭环（实测）

```text
正常          → 健康报告「事件总线(Redis) 正常」
停止 Redis    → 事件流「Redis 未连接, 事件总线已降级(不影响交易)」
                健康「系统正常, 无需操作(有 1 项降级: 事件总线(Redis))」
                can_buy 仍为 True —— 不假装正常, 也不误伤交易能力
恢复 Redis    → 35s 内自动接回, 事件流「Redis 已连接(事件总线可用)」, 健康恢复
```

> **修的是一个实测缺口**：`redis_status` 原本只在启动时判定一次，Redis 运行中挂掉应用完全无感，
> 健康报告会一直显示「正常」。补 `_redis_watch_loop`（15s 周期）后才成为真闭环。

### MySQL 不可达（实测）

停 MySQL 后 `/api/operator-status` 返回 **3206ms**，结论「数据库不可达 / 系统自动阻止交易」。

> 首版实测是 **12~20 秒** —— 底层建连一直等操作系统级 TCP 超时，页面转圈而不是说话。
> 修法：`database.py` 给 MySQL 建连显式 `connect_timeout=5`（管**所有**建连）+
> 库不可达时跳过逐项统计读取。**依赖不可用必须快速失败并如实说话。**

### 实测踩到的两个坑

1. **容器名冲突**：`container_name: adaptive-mysql` 与本机自建 MySQL 抢名字，`up` 直接失败。
   改为 `adaptive-trading-mysql` / `adaptive-trading-redis`（可 env 覆盖）。
   —— compose 不该抢占通用名字。
2. **测量陷阱**：改完代码后 `docker compose up --force-recreate` 因 MySQL 尚未 healthy
   而**没有真正替换容器**，连续两次量到的都是旧镜像。必须先等 `service_healthy` 再测。
