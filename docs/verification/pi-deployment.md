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
| DOCKER_COMPOSE_FULL | **PASSED**（V13 实测，见 [docker-verification.md](docker-verification.md) §V13） |
| TRADING_SAFETY | PASSED（单测） |
| RECONCILIATION | PASSED（单测） |
| EVIDENCE_CHAIN | **NOT_EXECUTED**（见下） |
| TESTNET | **PASSED**（V13 实测, 见 [local-verification.md](local-verification.md) §V13 测试网） |

> 结论（V13 更新）：**NOT_READY_FOR_PI** —— `DOCKER_COMPOSE_FULL` 已在 V13 通过，
> 但 `EVIDENCE_CHAIN` 仍未执行，且 Level 3 本身**没有执行能力**（本环境无 SSH 到 Pi）。
>
> ⚠️ 另一条与 Pi 有关的 V13 进展: Pi 上卡住的 `SAFE_MODE + KILLED` 根因已修 ——
> `POST /api/emergency/recover` 此前只解除急停标志, **不碰风险态与生命周期**,
> 所以冻结后只能靠重启进程脱身(见 `at50_risk/recovery_flow.py` 的模块说明)。
> 该修复**存在于仓库但尚未部署到 Pi** —— 与 V12.6 的 equity_drift 修复同一种情况。

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
