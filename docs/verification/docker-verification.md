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
