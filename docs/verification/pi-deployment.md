# Level 3 — Raspberry Pi ARM64 生产部署验证

> 三级验证的第三级。**只有 Level 1 + Level 2 达标后才进入本阶段。**

## 当前状态

```
PI_DEPLOY          NOT_EXECUTED
PI_ARM64_SMOKE     NOT_EXECUTED
```

**原因**：当前环境**没有 SSH/部署执行能力**（只能 HTTP 只读访问 Pi:8800）。

## ⚠️ Pi 当前跑的是旧镜像（只读实测证据）

2026-09-12 通过 HTTP 只读取到：

```
mode = live_testnet | status = KILLED | can_buy = False | can_sell = False
uptime ≈ 46688s       reconciled = False
last_error = 对账 / equity:equity_drift SOLUSDT
```

**两条结论**：
1. 响应里**没有 `trading_mode` 字段** ⇒ 旧镜像（V12.7 的三模式未部署）
2. 仍卡在 **V12.6 已修**的 `equity_drift` bug 上，已持续约 13 小时
   ⇒ **修复存在于仓库但从未部署**

## 进入本阶段的前置门（全部须 PASS）

| 门 | 状态 |
|----|------|
| LOCAL_UNIT | PASSED |
| LOCAL_INTEGRATION | PASSED |
| LOCAL_MODE_SWITCH | PASSED |
| DOCKER_BUILD | PASSED |
| DOCKER_SMOKE | PASSED |
| DOCKER_RESTART | PASSED |
| DB_PERSISTENCE | PASSED |
| DOCKER_COMPOSE_FULL | **NOT_EXECUTED** |
| TRADING_SAFETY | PASSED（单测） |
| RECONCILIATION | PASSED（单测） |
| EVIDENCE_CHAIN | **NOT_EXECUTED** |

> 结论：**NOT_READY_FOR_PI** —— `DOCKER_COMPOSE_FULL` 与 `EVIDENCE_CHAIN` 未过。

## 部署步骤（待执行）

```bash
ssh <pi>
cd <repo> && git pull && git rev-parse HEAD
export IMAGE_TAG=$(git rev-parse --short HEAD) GIT_SHA=$(git rev-parse HEAD)
docker compose --env-file /etc/adaptive-trading/production.env build
docker compose --env-file /etc/adaptive-trading/production.env up -d
docker compose ps
```

**判据**：
- `/api/operator-status` 出现 `trading_mode` 字段 ⇒ 新镜像已生效
- `reconcile_drift_pct` 回落 ⇒ V12.6 权益基线修复生效
- 容器 `GIT_SHA` == 仓库 HEAD ⇒ 版本可追踪

**⚠️ 急停是持久化的**：修复生效后仍需人工 `POST /api/emergency/recover`。
且这次它冻的是**真实问题**（旧 bug 导致的假漂移），不是误报。
