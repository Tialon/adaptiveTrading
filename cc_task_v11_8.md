# adaptiveTrading V11.8 — Docker 生产运行时 + 主网就绪自检(任务清单 + 执行记录)

> 目标: 把 V11.7「可验证、可审计、可复现的测试网证据」升级为「可在 Raspberry Pi 上通过 Docker
> 长期无人值守运行」的生产运行时, 并为「主网上线前最终安全审计」铺好**主网就绪自检**闸门。
> 产品边界继续冻结: Binance 单所 / SOLUSDT 单币 / Spot 现货 / 双仓 / 低频 / Python asyncio 单进程 /
> SQLite 单机生产(容器挂载卷)/ AI 只分析·提案、不直接下单。
> 禁止: 新币种 / Futures / 杠杆 / HFT / Kafka / K8s / 微服务 / Transformer / RL / RSI·MACD·BB 等新指标 /
> LLM 自动下单 / AI 绕过 TradingGate / 策略重构 / 「漂亮」式重构。
>
> 每单元完成即提交推送 main; 分支固定 main; `.env` 密钥绝不外泄; `logs/` 继续 gitignore。

---

## 0. 状态模型(沿用 V11.7 P0-1 七态)

```text
IMPLEMENTED     代码/测试已落地并钉死(但未在真实环境执行)
READY_TO_RUN    实现完成 + 前置条件满足, 可执行但尚未执行
EXECUTED        已在真实环境实际运行
PASSED          真实执行且验收通过
FAILED          真实执行但未通过验收
BLOCKED         真实执行被环境/前置条件阻断(非代码缺陷)
NOT_EXECUTED    尚未在真实环境执行
```

V11.8 跟踪项实际状态(截至本版本收口):

| 项 | 代码态 | 真实执行态 | 证据锚点 |
|----|--------|-----------|----------|
| 生产 Dockerfile(amd64) | IMPLEMENTED | **EXECUTED → PASSED** | 本地 amd64 镜像构建成功, 容器 init→TRADING→web server, `/api/health` 200, `docker stop` 0.81s exit 0 |
| 生产 docker-compose | IMPLEMENTED | **EXECUTED → PASSED** | `docker compose config --quiet` exit 0 |
| SQLite WAL/busy_timeout/foreign_keys | IMPLEMENTED | —(纯单测) | `test_v176_sqlite_pragmas.py` |
| MAINNET_READINESS_CHECK | IMPLEMENTED | —(纯函数+接线) | `test_v175_mainnet_readiness.py`(13 条) |
| CI Docker build + smoke | IMPLEMENTED | **NOT_EXECUTED** | 需推 main 触发 GitHub Actions 真实跑通(见 §P0-6) |
| ARM64(Pi)构建 | READY_TO_RUN | **NOT_EXECUTED** | 本机无 ARM64 宿主 + docker.io 被墙, 无法 buildx 多架构推送 |
| 测试网 7h/24h soak | READY_TO_RUN | **NOT_EXECUTED** | 沿用 V11.7 P1-8, 需真实挂机(见 testnet-operation.md) |

---

## 1. Phase 1 — 代码重新审计(§2, 完成)

「先检查当前 Git HEAD, 先审计现有代码, 不假设之前任务已完成」。开工前核查:

- HEAD `3df9e63`(V11.7 收口)→ 本轮从该基线起改。
- BUY/SELL 单一咽喉: 复核 `ExecutionEngine.execute()` 仍是唯一下单点(仅 `run.py::_on_signal` 与
  `run.py::_apply_core_action` 两处调用), 无新增 bypass —— 与 V11.7 P1-5 结论一致, 未发现绕过。
- 重启安全: 急停持久化 + 启动对账 + 测试网闸门均在位, 本轮未改交易语义, 仅加 SQLite 加固 +
  主网就绪自检 + Docker 包装。
- 启动序列: STARTING→DB→Exchange→Startup Reconciliation→Runtime Health→TradingGate→NORMAL
  未变; 主网就绪自检插在「主网守卫」之后、测试网闸门之前(见 §P0-4)。

## 2. P0-3 — SQLite 生产加固(WAL / busy_timeout / foreign_keys)(完成)

`at01_common/database.py` 用 SQLAlchemy `connect` 事件对 SQLite 连接统一施加生产级 pragma:

- `PRAGMA journal_mode=WAL`(读写并发 + 崩溃更安全, 适配 Pi 长期无人值守)。
- `PRAGMA busy_timeout=5000`(多写竞争下等待而非立刻抛 `database is locked`)。
- `PRAGMA foreign_keys=ON`(SQLite 默认关 FK, 显式开启保证外键约束)。
- 关键坑(aiosqlite): 底层 `sqlite3.Connection` 在 worker 线程创建且 `check_same_thread=True`,
  跨线程执行 pragma 会抛「objects created in a thread can only be used in that same thread」;
  故 `_ensure_engine()` 对 SQLite 追加 `connect_args={"check_same_thread": False, "timeout": 5.0}`,
  并新增 `_sqlite3_connection()` 解包辅助(兼容 `sqlite3.Connection` 与
  `AsyncAdapt_aiosqlite_connection.driver_connection._conn`), pragma 施加 wrapped try/except
  best-effort(裸测试引擎无该 connect_args 时跳过不炸)。
- 测试 `tests/unit/test_v176_sqlite_pragmas.py`(4 条): 非 SQLite 跳过 / 施加三 pragma /
  `create_async_engine` 真实连接验证 journal_mode=wal + busy_timeout=5000 + foreign_keys=1。

## 3. P0-4 — 主网就绪自检(MAINNET_READINESS_CHECK)(完成)

新建 `at01_common/mainnet_readiness.py`, 纯函数 + 报告格式化:

```python
mainnet_readiness_check(*, binance_testnet, paper_trading, live_trading_confirm,
                        api_scope_confirmed, symbol, config_problems, kill_switch_armed,
                        git_sha, base_url) -> dict  # {allowed, blocked_reasons, report}
```

八项判定(任一不满足 → BLOCKED 拒绝启动):

1. `BINANCE_TESTNET=false`(确连主网);
2. `PAPER_TRADING=false`(非纸面);
3. `LIVE_TRADING_CONFIRM=true`(大小写/空白不敏感);
4. `MAINNET_API_SCOPE_CONFIRM=true`(人工确认主网 key 仅 Spot、关提现/资金转移);
5. `symbol` 在 `SUPPORTED_SYMBOLS`("SOLUSDT");
6. `config_problems` 为空(配置审计已过);
7. `kill_switch_armed=false`(非急停冻结);
8. `git_sha` 非空 + `base_url` 含 `api.binance.com` 且不含 `testnet`(确连主网端点)。

接线在 `at01_common/wiring.py`: `BINANCE_TESTNET=false` 时, 主网守卫之后、测试网闸门之前执行,
打印 `=== MAINNET READINESS ===` 报告; 任一不满足 → `RuntimeError` 拒绝启动。
`kill_switch_armed` 此处传 `False`(持久化急停态尚未从 DB 载入), 由启动末尾
`kill_switch.is_armed` 单独守卫兜底, 两处互不重复。新增 settings 字段
`mainnet_readiness_enabled`(默认 true)与 `mainnet_api_scope_confirmed`(默认 false → 主网必被拦)。

> **绝对禁止在当前任务直接打开主网自动交易**: `MAINNET_API_SCOPE_CONFIRM` 默认 `false`,
> 主网就绪自检默认必 BLOCKED; 真 go/no-go 由 docs/mainnet-readiness.md 人工复审。
> 测试 `tests/unit/test_v175_mainnet_readiness.py`(13 条): 八维各自阻断、原因累积、报告格式、
> live_confirm 大小写/空白不敏感、全部通过 allowed=true。

## 4. P0-1/P0-2/P0-5 — Docker 生产运行时(完成)

- **Dockerfile**(多阶段): `python:3.13-slim` 基础(amd64+arm64 由 buildx `--platform` 指定);
  builder 用 `uv sync --frozen --no-dev --no-install-project` 从 `uv.lock` 可复现装依赖
  (扁平 atXX 目录无 build-system, 由 run.py bootstrap 注入 sys.path); 运行层 `tini` 作 PID 1
  (SIGTERM 转发 + 僵尸回收)、非 root `app` 用户、`STOPSIGNAL SIGTERM`、`ENTRYPOINT ["/usr/bin/tini","--"]`。
  无 .env/密钥进镜像(env 由 compose `env_file` 注入; `.dockerignore` 兜底)。构建参数
  `PYTHON_BASE/PIP_INDEX_URL/UV_DEFAULT_INDEX` 可覆盖(镜像站/依赖镜像站环境)。
- **docker-compose.yml**: 单容器 `adaptive-trading`, `restart: unless-stopped`, `env_file: [.env]`,
  数据/日志/证据分别挂载宿主机 `./data ./logs ./evidence`(**持久化, 非 tmpfs**), 端口 8800:8800,
  HEALTHCHECK(`/api/health` 200)。`health=OK` 仅表示 HTTP 可达, **不等于 can_buy**;
  交易许可仍由 TradingGate 单一权威判定。
- **.env.example + .gitignore**: 补 `TZ`/`WEB_ADMIN_TOKEN`/`RUN_TESTNET_TRADING`/
  `MAINNET_READINESS_ENABLED`/`MAINNET_API_SCOPE_CONFIRM`; `.gitignore` 增 `*.secret`/`secrets/`/
  `*.pem`/`*.key`/`data/`/`evidence/`。删除遗留 `at90_deploy/Dockerfile`、`at90_deploy/docker-compose.yml`
  (路径错误、缺 atXX 目录, 损坏文件)。

## 5. P0-7 — Docker 构建 + 冒烟 EXECUTE(EXECUTED → PASSED, amd64)

本会话本机(docker 29.4.1 / linux amd64)真实构建 + 冒烟:

- 镜像源 `docker.io` 与 `pypi.org` 在本环境被墙, 用 Dockerfile 构建参数指向可达镜像站:
  `--build-arg PYTHON_BASE=docker.1ms.run/library/python:3.13-slim`
  `--build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple`
  `--build-arg UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple` → 构建成功。
- 容器 `at-smoke` 启动: init → 连测试网 WS → TRADING → web server, 纸面成交一笔 BUY;
  `/api/health` 返回 200 `{"status":"ok","running":true}`。
- 优雅停机: `docker stop at-smoke` → 0.81s, exit code 0(SIGTERM → run.py graceful shutdown)。
- `docker compose config --quiet` exit 0。

## 6. P0-6 — CI Docker build + smoke(完成)

`.github/workflows/ci.yml` 新增 `docker-smoke` job: 构建镜像(`docker build -t adaptive-trading:ci .`)
+ 以最小 env(`PAPER_TRADING=true` + `BINANCE_TESTNET=true` + `API_HOST=0.0.0.0` +
`WEB_ADMIN_TOKEN=ci-smoke-token`)启动容器, 轮询 `/api/health` 200(90s 超时), 失败即 job 失败。
纸面+测试网不产生真实下单, 也不依赖交易所连通性(行情预热/WS 连接失败均为非致命降级)。

## 7. P0-8 — 文档(完成)

新增 `docs/docker-deployment.md`、`docs/raspberry-pi-deployment.md`、`docs/mainnet-runbook.md`、
`docs/mainnet-readiness.md`、`cc_task_v11_8.md`; 同步 README / progress / architecture /
module-map / runbook / production-readiness / testnet-operation / testnet-runbook / database-migration。

---

## 8. 验证(十八)

- **pytest**: `pytest tests/ -q -m "not testnet"` → **1236 passed, 6 deselected**;
  coverage **80%** ≥ 75%(8742 statements, 1784 missed)。
- **ruff**: `ruff check .` → All checks passed。
- **docker build**(amd64, 镜像站覆盖): 成功; **容器冒烟**: `/api/health` 200 + graceful shutdown 0.81s。
- **docker compose config**: `--quiet` exit 0。
- **testnet**: 真实测试网冒烟/下单仍 `-m "not testnet"` 排除于 CI; 本轮未跑真实下单(不伪造)。

## 9. 实际交付清单

**新增模块**:
- `at01_common/mainnet_readiness.py`(P0-4 主网就绪自检)

**修改模块**:
- `at01_common/database.py`(P0-3 SQLite WAL/busy_timeout/foreign_keys + check_same_thread)
- `at01_common/settings.py`(P0-4 `run_testnet_trading` 改用 settings + 主网就绪自检字段)
- `at01_common/wiring.py`(P0-4 接线主网就绪自检)

**新增文件**:
- `Dockerfile` / `.dockerignore` / `docker-compose.yml`(根, P0-1/P0-2)
- `.env.example`(P0-5)/ `.gitignore`(P0-5)/ `.github/workflows/ci.yml`(P0-6)
- `docs/docker-deployment.md` / `docs/raspberry-pi-deployment.md` / `docs/mainnet-runbook.md` /
  `docs/mainnet-readiness.md`

**删除**: `at90_deploy/Dockerfile`、`at90_deploy/docker-compose.yml`(损坏遗留)

**新增测试**(17 条): `test_v175_mainnet_readiness.py`(13)/ `test_v176_sqlite_pragmas.py`(4)。

**无新表无迁移**(领域 schema 不变)。

## 10. 诚实披露

- **ARM64(Pi)构建未执行**: 本机无 ARM64 宿主, 且 `docker.io` 被墙无法 buildx 多架构推送;
  Dockerfile 已按多架构设计(`python:3.13-slim` + buildx `--platform`), 但真实 Pi 构建待部署环境执行。
- **CI Docker smoke 未在 GitHub Actions 真实跑通**: 已写入 `ci.yml`, 需推 main 触发(本会话无法
  观测远程 runner 结果), 状态 NOT_EXECUTED。
- **主网未开启**: 本轮仅落地主网就绪自检闸门(默认 BLOCKED), 未直接打开主网自动交易。
- **测试网 7h/24h soak 仍 NOT_EXECUTED**: 沿用 V11.7 结论, L3 未达, 不因 Docker 就绪而虚报。
- **本地 amd64 冒烟为纸面成交**: 冒烟用 `PAPER_TRADING=true`, 未做真实测试网下单闭环。
