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
| compose 全流程 | `docker compose up/down` | **NOT_EXECUTED** |
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
