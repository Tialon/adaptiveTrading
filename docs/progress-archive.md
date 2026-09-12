# 项目进度归档（V12.1 及更早）

> ⚪ **历史存档** —— 记录每个版本当时交付了什么、验证到什么程度。
> **不代表当前状态**；当前态见 [progress.md](progress.md)。
>
> 归档内容里的包名是**当时的旧名**（2026-09-12 做过一次按阅读顺序的重编号），
> 映射表见 [tasks/README.md](tasks/README.md)。

---

## V12.1 — 工程收口审查（已完成，2026-09-09）

**审查基线**: `main` @ `cfa552a`（V12 小资金主网接管）。本轮仅做静态审查 + 类型/CI 收口 + 回归，
**未触发测试网或主网交易，主网状态仍为「代码就绪 / 未上线」**。

### 已完成（P0 → P1 → P2）

- **P0 恢复 CI 绿灯**: 删除 `test_v12_db_backup.py` 未用 `import pytest`、`test_v12_risk_params.py`
  未用 `TieredDrawdownManager` 导入；`uv run ruff check .` exit 0。
- **P1 可空值契约收口**（6 文件 24 mypy error → 0）: 执行/恢复/启动对账链路每个需交易所查询的入口
  先 fail-closed（依赖缺失返回未解决/受控结果，保持禁开仓），守卫后绑定局部变量再调用，不靠
  `cast`/`# type: ignore` 消音：
  - `startup_reconciler.py` `_self_heal_filled`/`_resolve_unknown`：`rest is None` → 返回 False；
  - `order_recovery.py` `_recover_status`/`_recover_buy_accounting`/`_recover_sell_accounting`/
    `_mark_canceled`：`rest`/`execution` 缺失返回未解决/直接返回；
  - `execution_executor.py` `_resolve_unknown`/`_confirm_fill`/`_ingest_fills`：`rest is None` fail-closed；
    `apply_recovered_fill` 在 `exchange_order_id` 为空时**不把 None 传入查询接口**，仅用订单级数据并留审计日志；
  - `execution_state.py:169`：`"set[str]"` 标注被类内 `set` 方法遮蔽 → 改 `Set[str]`（typing）；
  - `exchange_truth_reconciler.py:154-155`：拒绝空/非 datetime 时间戳后再取最小值 + UTC 转换；
  - `cross_reconciler.py:95`：`_m` 的 `expected/actual` 放宽为 `Any`，非数值字段也能构造可审计差异。
  - 补测 `test_v121_null_contract.py`（9 条）: 依赖缺失 fail-closed / `exchange_order_id=None` 不查询
    不伪造成交 / 空非法时间戳与金额不崩溃。

### 验证

- **测试 1298/1298 全绿**（+6 testnet opt-in，CI 排除）；coverage **79.92%** ≥ 75%；`ruff` 全绿；`mypy` 0 errors。
- **主网仍未上线（诚实披露）**: 本轮未触发任何真实测试网/主网交易；项目仍为「代码就绪 / 主网未上线」态。

## V12 — 小资金主网接管(代码就绪, 主网未上线, 2026-09-09)

**定位: 按 V12 任务书(42 节)把「已冻结的测试网运行时」接管到 Binance 主网 SOLUSDT 现货,
以极小资金(约 ¥1 万等值 SOL)做低买高卖, 无人值守长期运行。冻结不变(单所/单币/现货/双仓/
低频/AI 只提案); §38 严格顺序推进; 每单元完成即提交推送 main。**

### 已完成(代码 + 测试)

- **§16-19 风控参数接线** `at50_risk/risk_tiered.py`: 分级回撤四档阈值从 settings 读取
  (`risk_drawdown_observe/reduce/pause_pct` + `risk_max_drawdown`), 消除死配置; §18 日内亏损 3%
  → REDUCE_ONLY(禁开新仓、保留卖出), §19 回撤 15% → KILL 急停(非自动复位)。
- **§24-25 HODL 基准** `at70_journal/hodl_benchmark.py` + `models.HodlBenchmarkState`(单行表):
  接管时刻冻结「初始权益/初始 SOL 数量/初始 SOL 价格」, `compute_benchmark` 算 Adaptive/HODL/Cash
  权益与 Alpha; 基线只记一次、冻结不可覆盖。`SCHEMA_VERSION=V12.0`, `test_v129` 锚点同步。
- **§10-11 主网只读接管** `at60_execution/mainnet_takeover.py`: 首次主网启动只读快照(余额/SOL/
  挂单/成交历史)+ 对账(意外挂单/持仓漂移 → 急停冻结), 既有 SOL 视为初始持仓记基线; 接线 `wiring.py`
  (仅主网实盘 + 尚无基线时执行)。
- **§5 局域网 Web** `docker-compose.yml`: 端口 `127.0.0.1:8800:8800` → `8800:8800`(暴露局域网);
  鉴权仍由 `WEB_ADMIN_TOKEN` fail-closed 兜底(非回环 `API_HOST` + 空 token → validate 拒绝启动)。
- **§37 每日复盘扩展** `at70_journal/daily_report.py` + `run.py`: 新增「账户与持仓 / HODL 对标 /
  交易活动」三节 + 交易门/对账健康; `run._v12_report_metrics` 组装账户/HODL 指标(纯本地, 不查交易所)。
- **§31 SQLite 备份** `scripts/db_backup.py`: `PRAGMA integrity_check` + 在线备份(WAL 安全,
  标准库 `Connection.backup`)+ 保留清理; 配 `tests/unit/test_v12_db_backup.py`。
- **§32 启动前清单** `docs/mainnet-prestart-checklist.md`: 备份/配置/API 权限/只读接管/go-no-go。

### 验证

- 测试 **1289/1289 全绿**(+6 testnet opt-in); 新增 test_v12_* 覆盖(主网接管/HODL/每日复盘/备份)。
- **主网未上线(诚实披露)**: 主网真实只读验证 + 极小资金 BUY/SELL **未执行** —— 2026-09-09 操作者
  决定「跳过主网, 只做收口」, 未核对 §8 主网 API key 权限、未部署 Pi 主网 `.env`。项目停留在
  「代码就绪 / 主网未上线」态, 未触碰真实资金。将来如需上线, 按
  [mainnet-prestart-checklist.md](mainnet-prestart-checklist.md) + [mainnet-readiness.md](mainnet-readiness.md)
  逐项执行。

## V11.8 — Docker 生产运行时 + 主网就绪自检(已完成, 2026-09-09)

**定位: 把 V11.7「可验证、可审计、可复现的测试网证据」升级为「可在树莓派上通过 Docker 长期
无人值守运行」的生产运行时, 并为「主网上线前最终安全审计」铺好主网就绪自检闸门。不开发新策略;
冻结不变(单所/单币/现货/双仓/低频/AI 只提案)。每单元完成即提交推送 main。**

### P0 — SQLite 加固 / 主网就绪自检 / Docker 运行时 / CI

- **P0-1 Dockerfile + .dockerignore**: 多阶段 `python:3.13-slim`(amd64+arm64)+ builder
  `uv sync --frozen --no-dev --no-install-project` + 运行层 tini PID1 + 非 root app 用户 +
  `STOPSIGNAL SIGTERM`; `.dockerignore` 裁剪上下文 + 密钥防泄漏。
- **P0-2 docker-compose.yml**: 单容器 `adaptive-trading` + `restart: unless-stopped` +
  `env_file: [.env]` + `./data ./logs ./evidence` 持久化卷 + HEALTHCHECK; 删除遗留损坏
  `at90_deploy/Dockerfile`、`at90_deploy/docker-compose.yml`。
- **P0-3 SQLite 生产 pragma** `test_v176_sqlite_pragmas.py`(4 条): `database.py` 用 connect 事件施加
  `journal_mode=WAL` + `busy_timeout=5000` + `foreign_keys=ON`; 解 aiosqlite 跨线程坑
  (`check_same_thread=False` + `_sqlite3_connection()` 解包)。
- **P0-4 MAINNET_READINESS_CHECK** `test_v175_mainnet_readiness.py`(13 条): 新建
  `at01_common/mainnet_readiness.py`, 八维确定性判定(连主网/非纸面/显式确认/API 权限确认/单币/
  配置审计/非急停/git_sha+主网端点), 主网启动前强制, 任一不满足 BLOCKED; 接线 `wiring.py`。
- **P0-5 .env.example + .gitignore**: 补 TZ/WEB_ADMIN_TOKEN/RUN_TESTNET_TRADING/
  MAINNET_READINESS_ENABLED/MAINNET_API_SCOPE_CONFIRM; `.gitignore` 增 `*.secret`/`secrets/`/
  `*.pem`/`*.key`/`data/`/`evidence/`。
- **P0-6 CI docker-smoke job**: `ci.yml` 加镜像构建 + 最小 env 启动容器轮询 `/api/health` 200。
- **P0-7 Docker build + smoke EXECUTE**(amd64): 本地镜像构建成功 + 容器 init→TRADING→web server,
  `/api/health` 200 + `docker stop` 0.81s graceful(镜像站覆盖 `docker.1ms.run` + tsinghua PyPI)。
- **P0-8 文档**: 新增 docker-deployment / raspberry-pi-deployment / mainnet-runbook /
  mainnet-readiness / cc_task_v11_8; 同步 README/progress/architecture/module-map/runbook/
  production-readiness/testnet-operation/testnet-runbook/database-migration。

### 验证

- 测试 **1236/1236 全绿**(+6 testnet opt-in, CI 排除); coverage **80%** ≥ 75%; ruff 全绿。
- docker build(amd64)+ 容器冒烟 `/api/health` 200 + graceful shutdown 0.81s; `docker compose config` exit 0。
- 就绪等级 **仍 L2**; ARM64(Pi)构建 + CI smoke + 测试网 7h/24h soak 均 NOT_EXECUTED(诚实披露)。

## V11.7 — Testnet Evidence & Operational Hardening(已完成, 2026-09-08)

**定位: 把 V11.6 的「可运行基础设施」升级成「可验证、可审计、可复现的 Testnet Operational Evidence」。
不开发新策略; 冻结不变(单所/单币/现货/双仓/低频/AI 只提案)。每单元完成即提交推送 main。**

### P0 — 状态模型 / 优雅停机 / 验收契约 / 证据元数据

- **P0-1 状态模型**: 区分「代码已实现」与「真实环境已执行」, 统一七态
  `IMPLEMENTED / READY_TO_RUN / EXECUTED / PASSED / FAILED / BLOCKED / NOT_EXECUTED`(docs 收口)。
- **P0-2 Soak graceful shutdown** `test_v167_soak_shutdown.py`(12 条): `soak.py` 停机阶梯从裸
  `terminate()` 改为「POST /api/shutdown → 等待优雅退出 → 超时 terminate → 再超时 kill」, 失败/中断安全处理。
- **P0-3 Soak Acceptance Contract** `test_v168_soak_acceptance.py`(13 条): `evaluate_soak_result(...)`
  纯逻辑验收输出 `PASS/FAIL/BLOCKED`;「can_buy 曾经 false」不直接视为失败(降级禁买可能是正确安全行为)。
- **P0-4 Runtime Evidence 可复现元数据** `test_v170_soak_metadata.py`(9 条): evidence 回答「哪版代码/什么配置/什么环境」,
  新增 run_id / git_sha(真实 `git rev-parse`)/ start/end / requested/actual duration / symbol /
  paper_trading / binance_testnet / final_state / acceptance_result; 目录 `logs/soak/<run_id>/`。

### P1 — 迁移加固 / 证据链 / 测试网闸门 / 下单复审 / 一致性

- **P1-1 Migration checksum** `test_v169_db_migration_checksum.py`(6 条): `schema_version` 加 SHA-256
  checksum; 同版本同 checksum OK / 同版本异 checksum FAIL FAST(禁静默接受被修改的已执行迁移)。
- **P1-2 Migration concurrency safety** `test_v171_migration_concurrency.py`(2 条): 进程内
  `asyncio.Lock`(按 running loop 惰性取锁)+ 跨进程由 `schema_version.version` PK 兜底。
- **P1-3 Testnet evidence consistency** `test_v172_evidence_chain.py`(16 条): 新建
  `at01_common/evidence_chain.py` —— `build_evidence_chain`(run_id→order→fill→position→lot→
  sell_allocation→exchange_truth→reconciliation→soak_result 证据链)+ `chain_consistency_issues`
  (orphan_fill/fill_mismatch/buy_lot_mismatch/…)+ `load_run_evidence`。
- **P1-4 Testnet real execution gate** `test_v173_testnet_gate.py`(10 条): 新建
  `at01_common/testnet_gate.py` —— 真实执行须 `BINANCE_TESTNET=true` + `PAPER_TRADING=false` +
  `RUN_TESTNET_TRADING=1` + `live_trading=false` + 测试网 key 齐备, 否则 BLOCKED; **绝对禁止主网误执行**。
- **P1-5 BUY 全路径复审**: 重搜 create_order/BUY/place_order/submit_order + 反射动态分发核查;
  结论 **无 bypass**(`ExecutionEngine.execute()` 唯一下单咽喉, 仅 run.py 两处经闸门调用; Web 无下单端点;
  AI 只写 ai_advices)。
- **P1-6 Runtime health ↔ evidence 一致性** `test_v174_runtime_health_evidence_consistency.py`(12 条):
  锁定 `health.can_buy == gate.can_open_position()[0]` / `health.can_sell == gate.can_reduce_position()[0]`,
  九维阻断逐字一致、绝不虚报「可买」。

### P1-7~P1-9 — 真实测试网执行与 readiness 判定

- **P1-7 real Testnet preflight**(EXECUTED → PASSED): 本会话测试网可达, 真实跑
  `test_v152_testnet_order_lifecycle.py`(2 passed in 27.37s): LIMIT no-fill→cancel + MARKET BUY 0.072 SOL
  @103.4 + MARKET SELL @103.39, 三重主网守卫全程在位, 单笔交易所真相对账 PASS。
- **P1-8 7h/24h soak**(NOT_EXECUTED): 需真实挂机 7/24h, 本会话未执行, 不伪造。
- **P1-9 readiness 严格判定**: 仅一次真实 BUY/SELL → **仍 L2**(不虚报 L3); L3 需 7h soak PASS。

### 验证

- 测试 **1219/1219 全绿**(+6 testnet opt-in, CI 排除); coverage **79.45%** ≥ 75%; ruff 全绿。
- 就绪等级 **L2(运行时验证就绪)**; 真实订单闭环已 PASSED 但仍 L2(7h/24h soak NOT_EXECUTED)。

## V11.6 — Testnet Operational Validation & Financial Truth Hardening(已完成, 2026-09-08)

**定位: 从「代码就绪」迈向「测试网已验证 + 财务真相闭环 + 可启动无人值守观察」。不开发新策略/
新币种/合约/HFT/LLM 自动下单; 冻结不变(单所/单币/现货/双仓/低频/AI 只提案)。每单元完成即提交推送 main。**

### P0 — 财务真相与 BUY 安全契约

- **P0-1 真实 Binance Testnet 完整交易验证**: 走通「下单→成交→账本→对账」全链路(`test_v152` opt-in)。
- **P0-2 BUY Safety Contract** `test_v160_buy_safety_contract.py`: 停机/关键任务两维收口到统一闸门(单一权威),
  与 `_on_signal` 早退语义一致, 防停机窗口在途信号偷建仓。
- **P0-3 Live Financial Truth Audit** `test_v161_live_financial_truth.py`: AccountLedger 仅纸面落库,
  实盘财务真相锚定交易所对账链(ExchangeTruth + Position + Cross → ReconciliationMatrix), 跨源资金级差异单源即 KILLED。

### P1 — 运行时契约 / 迁移 / CI / 运维手册 / Soak

- **P1-1 AccountLedger Live Mode Design**(docs): `LIVE_ACCOUNT_LEDGER_MODE = EXCHANGE_TRUTH_RECONCILIATION`。
- **P1-2 Runtime Health Contract** `test_v162_runtime_health_contract.py`: `health.can_buy/can_sell` 直接取自统一闸门。
- **P1-3 RuntimeSupervisor 第二轮审计** `test_v163_runtime_supervisor_audit2.py`: 回调异常/取消/无重启契约。
- **P1-4 DB Migration 最小真正实现** `test_v164_db_migration.py`: `migrations.py` 前向 DDL 幂等应用 +
  `migrations/001_baseline.sql` + `schema_version` 簿记表 + `init_db()` 接线(不引入 Alembic)。
- **P1-5 CI Dependency Single Source**: ci.yml `uv sync --frozen --extra dev` 取代硬编码 pip 列表。
- **P1-6 Testnet Operational Runbook**: `docs/testnet-runbook.md`(启动自检/7 态监控/财务真相验证/告警/证据/停机)。
- **P1-7 7~24h 无人值守 Soak** `test_v165_soak.py`(11 条): `at01_common/soak.py` 一键 soak runner;
  **诚实披露: 本会话测试网不可达(smoke 报 Cannot connect), 真实 soak 未执行**(见 docs/testnet-operation.md)。
- **P1-8 运行时证据记录**: soak 逐行 health 快照落盘 `logs/soak-evidence.jsonl` + 迁移检测 + 摘要。

### P2 — run.py 瘦身(轻量抽取)

- `at01_common/bootstrap.py`(`inject_sys_path`)/ `wiring.py`(`wire_system` 承接 initialize 装配)/
  `runtime.py`(`run` 承接 main 生命周期); `run.py` 1250→995 行, 交易语义方法原样保留、不微服务化;
  `test_v166_run_slim.py`(6 条)锁「结构不回归」; coverage 对 wiring/runtime 与 run.py 同理由 omit。

### 验证

- 测试 **1139/1139 全绿**(+6 testnet opt-in, CI 排除); coverage **79.14%** ≥ 75%; ruff E9+F 全绿。
- 就绪等级 **L2(运行时验证就绪)**; L3(测试网无人值守实盘)因真实 7~24h soak 未执行(网络不可达)而未触发。

## V11.5 — Operational Production Hardening(已完成, 2026-09-08)

**定位: 从「测试证明正确」迈向「生产运行可靠」。不开发新策略; 产品冻结不变
(单所/单币/现货/双仓/低频/AI 只提案)。补齐「运维加固」五件套: 运行时任务监督、
统一运行时健康快照、故障注入、类型/静态审计、依赖/供应链。每单元完成即提交推送 main。**

### P0 — 运行时安全与监督

- **P0-1 Web 安全** `test_v150_web_security.py`: 写接口统一 `X-Admin-Token` 头鉴权(空令牌锁死/错令牌 401);
  `API_HOST=127.0.0.1` 出厂默认; 局域网/公网(`0.0.0.0`)需非空 `WEB_ADMIN_TOKEN` 否则 `validate()` fail-fast。
- **P0-2 RuntimeSupervisor** `test_v151_runtime_supervisor*.py`: 统一 spawn 命名后台任务、跟踪运行/完成/取消/异常;
  critical 任务异常退出 → 同步回调进安全态 + 急停; graceful shutdown 幂等取消回收。
- **P0-3 测试网真实下单闭环** `test_v152_testnet_order_lifecycle.py`(opt-in `RUN_TESTNET_TRADING=1`, CI 排除):
  交付真实下单全生命周期验证代码(下单→成交→账本→对账)。**实际执行属运维动作、本次未触发** → 诚实不升 L3。
- **P0-4 数据库迁移** `test_v153_schema_check.py`: 无迁移框架(create_all 只建不 ALTER)记为已知缺口,
  以 schema 稳定性锚点 + 全列 inventory 兜底, 捕获 create_all 静默列漂移; 迁移手册统一收口 `database-migration.md`。

### P1 — 运维加固

- **P1-1 运行时健康快照** `test_v154_runtime_health.py`(14 条): `build_runtime_health` + 七态分类器
  (KILLED/RECOVERY/PAUSED/REDUCE_ONLY/DEGRADED/TRADING/SAFE), `/api/metrics` 一个词回答「现在能不能交易、为什么不能」。
- **P1-2 故障注入** `test_v155_fault_injection.py`(4 条): critical 任务崩溃 → SAFE_MODE + 禁 BUY;
  停机中在途信号丢弃不触达执行引擎; WS 断连禁开仓→重连收敛。
- **P1-3 类型/静态审计**: mypy 接入(适度, 核心模块 only, 非 CI 门禁)——修复 6 处真实缺陷
  (漏 await 风控事件 / None 解引用 / 错误返回注解 / 动态注入未声明); ruff 移除 F401 全局忽略, 批量清理 28 处真实未使用导入。
- **P1-4 依赖/供应链**: pyproject 显式声明 `cryptography` / `websockets`(运行时必需可选传递依赖);
  `uv.lock` 重新解析同步(52 包); `pip-audit` 应用运行时依赖 0 已知漏洞(仅 pip 25.3 构建工具有 6 CVE, 非运行时)。
- **P1-5 生产就绪**: 重定义 L1~L4 就绪等级, 诚实判定仍为 **L2(运行时验证就绪)**; 重写 `production-readiness.md` 就绪清单。
- **P1-6 回归测试**: 全量 1085 非 testnet 测试全绿 + 6 testnet(opt-in, CI 排除)+ coverage 79.40% ≥ 75% + ruff E9+F 全绿。
- **P1-7 文档同步**: README/architecture/progress/module-map/runbook 对齐 V11.5; 测试数 1032→1085。
- **P1-8 最终审查 + 保存进度 + Git 收口**: 最终审查、更新记忆、git status 与 push 收口。

### 验证

- 测试 **1085/1085 全绿**(+6 测试网 opt-in, CI 排除); coverage **79.40%**(8175 stmt / 1684 missed)≥ 75% 阈值; ruff E9+F 全绿。
- mypy 为「建议性」检查: 剩余 24 处动态注入 union-attr/arg-type 噪声, 不纳入 CI 门禁。
- 就绪等级 **L2(运行时验证就绪)**; L3(测试网无人值守实盘)需在部署环境真实跑 `run.py`
  (`RUN_TESTNET_TRADING=1`)观察, 属运维部署验证、非代码交付, 故不虚报 L3。

## V11.4 — Real Runtime Validation(已完成, 2026-09-08)

**定位: 从「测试证明正确」迈向「长跑证明正确」。不信任 V11.3 文档自述, 从当前 main 重新审计;
产品冻结不变(单所/单币/现货/双仓/低频/AI 只提案)。每单元完成即提交推送 main。**

### P0 — 运行时验证(不变量)

- **P0-2 长跑 Soak 测试** `test_24h_soak.py`: 24 周期(简单+多 lot)+ 6 类故障注入(WS 断开/REST 超时/重复成交/部分成交/部分成交后撤单/未知订单), 逐周期断言 5 条财务不变量。
- **P0-3 异常绝不继续 BUY** `test_v142`: `can_buy` 全路径(急停/熔断/风险态/资金熔断闸门)钉死。
- **P0-4 恢复状态机穷举审计** `test_v143`: NORMAL/REDUCE_ONLY/PAUSED/KILLED/RECOVERY_CHECK 全转移。
- **P0-5 重启/崩溃 Soak**: 急停持久化 + 重启不自动复位 + 状态收敛。
- **P0-6 真实测试网只读冒烟** `test_testnet_smoke.py`: 连通/规则/行情/鉴权(opt-in, CI 排除)。
- **P0-7 数据库迁移设计审计** `test_v129`: schema 全列清单钉死(25 表 / 259 列, 捕获 create_all 静默列漂移)。
- **P0-8 run.py 运行时监督审计** `test_v130`: 修复 6 处局部导入 NameError 死路径(熔断决策/对账 KILL/双仓/core ADD·REDUCE/告警)。

### P1 — 生产就绪

- **P1-1 CI 加强**: ruff 正确性基线(E9+F, 忽略 F401)+ coverage 阈值 75%(实测 78.9%)。
- **P1-2 Testnet Smoke 与 CI 隔离**: CI `-m "not testnet"` 显式排除 + fixture env 双保险。
- **P1-3 生产就绪等级更新**: 引入 L1~L4 等级, 当前 L2(运行时验证就绪), 见 `production-readiness.md`。
- **P1-4 运行报告机制**: 每日复盘附「运行状态」快照(生命周期/风险态/急停/熔断/告警)。
- **P1-5 Observability 最终检查**: 消除死指标(8 写而不读计数器进 snapshot + data_gaps 读而不写接线)。
- **P1-6 静态审计**: TODO/FIXME/bare-except 零; 2 处 `except:pass` 加注释说明为可选旁路/尽力而为。
- **P1-7 文档同步**: README/architecture/progress 版本 V11.3→V11.4; module-map 测试数 885→1032 并补 V11.3/V11.4 测试文件; production-readiness/runbook 测试数 1021→1032、覆盖率 78.5%→78.9%。
- **P1-8 测试执行**: 最终全量 1032/1032 全绿 + coverage 78.95% ≥ 75% + ruff 全绿(169.65s)。
- **P1-9 最终安全审查**: `.env` 已 gitignore(仅 `.env.example` 占位符入库), 仓库/历史无硬编码密钥; 生产源码无 eval/exec/pickle/os.system/subprocess/__import__; 主网守卫 + 纸面默认 + fail-fast 在位。披露 Web 面板 `0.0.0.0:8800` 无鉴权为已知限制(单机设 `API_HOST=127.0.0.1` / 局域网加反向代理+鉴权)。
- **P1-10 不要扩展产品**: 冻结复核通过——单所 Binance / 单币 SOLUSDT(`SUPPORTED_SYMBOLS` 钉死 + 非 SOLUSDT fail-fast)/ 现货(仅 `market_futures_client` 只读资金费率+持仓量作情绪数据, 无合约下单)/ 双仓 / 低频 / AI 只提案(优化器无 `.activate(` 调用方)。无 RSI/MACD/BB/Transformer/RL/HFT。

### 验证

- 测试 **1032/1032 全绿**(+4 真实测试网冒烟 opt-in); 覆盖率 78.9% ≥ 75% 阈值; ruff 全绿。
- 就绪等级 **L2(运行时验证就绪)**, 下一步 L3(测试网无人值守实盘)属运维部署验证, 不在代码交付内。

## V11.3 — Production Hardening / Long Running Reliability(已完成, 2026-09-08)

**定位: V11.2 后冻结产品不变(不新增策略/币种/合约/高频/LLM 下单), 专做「生产加固 / 长跑可靠 /
配置正确 / 文档真实」。每单元完成即提交推送 main。**

### P0 — 正确性加固

- **P0-1 全组件审查**: 15 组件逐项核对。
- **P0-2 Symbol 产品定义一致**: 冻结 `SUPPORTED_SYMBOLS=("SOLUSDT",)`, 非 SOLUSDT fail-fast。
- **P0-3 Settings 全量 fail-fast**: `validate()` 覆盖 ~17 字段(实盘缺 key / 空标的 / 三桶比例 / 非法阈值)。
- **P0-4 TradingGate 最终审计**: 入口全覆盖 + 防绕过。
- **P0-5 Recovery 状态机压力审计**: restart-oriented。
- **P0-6 Long Running Simulation**。
- **P0-7 Memory/Task Leak Audit**: `_pending_tasks` 追踪 + `flush_events` gather 等待在途事件落库。
- **P0-8 Database Consistency Audit**: `test_v137_db_consistency`(25 表 schema vs metadata + 唯一约束)。
- **P0-9 Fee Accounting 最终审计**: `test_v138`(跨路径逐位一致 + 费用恰好一次 + SOL 折算)。
- **P0-10 Observability Hardening**: 样本有界(10k)+ 恢复计数接线(修复 recovery_streak 死指标)+ 告警降噪。

### P1 — 生产就绪

- **P1-1 Metrics 持久化评估**: 保持内存 `MetricsStore`, 不新增表/不引 Prometheus(`docs/metrics-persistence.md`)。
- **P1-2 架构文档事实同步**: 12→25 表、MySQL→SQLite 默认、补齐模块清单(`docs/architecture.md`)。
- **P1-3 文档一致性审计**: SOLUSDT / 六态 regime / 872 测试 / SQLite 默认(.env.example / docker-compose / README / runbook)。
- **P1-4 默认禁主网**: `mainnet_blocked_reason()` 守卫可测试化 + `test_v140`。
- **P1-5 最小 pytest CI**: `.github/workflows/ci.yml`(main push + PR, Python 3.13)。
- **P1-6 静态代码审计**: TODO/FIXME/bare-except 零; 修复启动自愈 `except: pass` 误标取消 bug。
- **P1-7 Optimizer 只产 proposal**: `test_v141`(snapshot active=False + activate 显式)。
- **P1-8 生产就绪检查清单**: `docs/production-readiness.md` + README 文档索引。
- **P1-9 冻结不变守约**: 验证单币/四策略/情绪·HMM 默认关/无 AI 下单路径。
- **P1-10 最终测试**: 885 全绿(ruff/mypy 未安装, 属可选)。

### 验证

- 测试 **885/885 全绿**(含故障注入 / 长跑 / 混沌 / 财务不变量 / 主网守卫 / 提案守约)。
- 无新增交易策略 / 币种 / Futures / 高频 / LLM 下单(冻结不变守约)。

## V11.2 — System Integration & Production Readiness(已完成, 2026-09-08)

**定位: V11.1 交付了 10 个「独立测试过」的资金正确性/自愈模块, 但多数未接入主运行链路。
V11.2 不再开发新模块, 而是做 System Integration / End-to-End Verification / Production Readiness:
把它们真正接进 run.py, 用端到端故障注入与财务不变量证明「异常下不错误改账、不错误开仓」。**

### P0 — 主链路集成与正确性

- **P0-1/P0-2 统一 CanTrade**: 新建 `at50_risk/trading_gate.py` 单一权威交易闸门, 组合六维
  (生命周期 + 风险态 + 行情健康 + 交易所健康 + 对账健康 + 资金熔断), 三接口
  `can_open_position` / `can_reduce_position` / `can_cancel_order`; `run.py` 的 `_on_signal` 与
  `_apply_core_action` 统一改走闸门。回归 20 条 → 699/699。
- **P0-3 漂移定义正确性**: 新建 `at60_execution/drift.py` 精确定义 equity/position/cash 三向漂移语义,
  消除「missing-data 当 0 drift」反模式(`local_equity<=0`/`truth_complete=False` → 不可信不 0)。
  回归 14 条 → 713/713。
- **P0-4 CircuitBreaker 真正接入**: `_reconcile_loop` 拉交易所账户算三向真相 → `compute_drift` →
  `FundCircuitBreaker.assess` → 单一处置点 `_apply_breaker_decision`(REDUCE_ONLY/PAUSE/KILL)+
  审计落库 `RiskEvent`; `_apply_verdict` 反馈对账/交易所健康到闸门, 消除「默认健康假设」。回归 10 条 → 723/723。
- **P0-5 端到端故障注入**: `test_v124_fault_injection.py` 用真实闸门栈对 24 类故障注入, 断言最终态 +
  核心不变量「故障绝不『异常 → 继续 BUY』」。回归 26 条 → 749/749。
- **P0-6 财务不变量最终审计**: `test_v125_financial_invariants.py` 收口 5 条守恒不变量(允许手续费)
  + 「守恒破坏 → 禁开仓」系统级不变量。回归 6 条 → 755/755。

### P1 — 生产就绪

- **P1-1 真实运行生命周期**: `SystemLifecycle` 增加迁移审计轨迹 `history` + `last_transition_at`;
  新增纯函数 `apply_reconcile_verdict` 把对账判定映射为生命周期迁移; `run.py::_apply_verdict` /
  `_apply_breaker_decision` 驱动运行期 TRADING→DEGRADED→RECOVERY→READY→TRADING、*→SAFE_MODE。
- **P1-2 Observability 真正接入**: 新增采集器纯函数 `record_execution` / `record_reconcile_verdict` /
  `record_breaker_action`; `run.py` 各循环喂入指标(执行延迟/失败率/对账漂移/数据缺口/策略归因/熔断计数);
  新增 `GET /api/metrics`; `_risk_loop` 每 5s `evaluate_alerts` 阈值告警。回归 22 条 → 777/777。
- **P1-3 Backtest 最终验收**: `test_v127_backtest_acceptance.py` 多市场态合成数据验收 V7 真实策略管线
  (bar 全覆盖 / 对账 balanced / 曲线合法 / 指标有限)。→ 779/779。
- **P1-4 Production Configuration Audit**: `Settings.validate()` fail-fast 拦截「实盘缺 key / 空标的 /
  三桶比例和≠1」; `run.py` 启动早期校验未过即拒绝启动。回归 9 条 → 788/788。
- **P1-5 数据库迁移审计**: 无迁移框架(create_all 只建不 ALTER)记为已知缺口; 以「schema 稳定性锚点」兜底
  —— `SCHEMA_VERSION` 标记 + `test_v129_schema_audit.py` 钉死 25 表清单与资金守恒关键列。→ 792/792。
- **P1-6 代码死路径审计**: 清除死模块 `at01_common/time.py`(无任何引用); 2 处 legacy 但仍有入口
  路径保留并文档化(backtest_engine / backtest_walkforward)。→ 792/792。
- **P1-7 文档同步**: README / progress / architecture / runbook 全部对齐 V11.2; 冻结不变重申。

**验证**: 全量 **792/792** 通过; 分支 main, 逐单元提交推送。详见 `cc_task_v11_2.md`。

## V11.1 — Financial Correctness & Self-Healing(已完成, 2026-09-08)

**定位: 承接 V11.0「证明异常下不错误改账」, 补上 Exchange Truth 完整性、手续费计价、账本重建、
SELL 自愈、对账分级五大资金正确性闭环。**

### P0-1 Exchange Truth V2(已完成)

- `get_my_trades_all` 返回 `MyTradesResult`(去重 + 重复/跳号/翻页耗尽检测), 不再静默假定「已拉全」。
- 交易所真相对账窗口从本地订单 `created_at` 推导(减 60s buffer), 去掉硬编码「15min / 200 笔」。
- 分页耗尽 → `truth_incomplete` / `pagination_exhausted`, 只降级(pause 自动恢复)不冻结;
  `trade_duplicate` / `trade_id_gap` 仅可观测性告警(去重已消除资金影响, 跳号在 myTrades 中属正常)。
- 不完整时跳过逐订单成交核对与孤儿检测, 避免误判 `fill_truth_missing` / `fill_truth_mismatch` / `orphan_trade`。
- **新增测试 9 条**, 全量 **521/521** 通过。

### P0-2 Fee Accounting Contract(已完成)

- 新建 `at60_execution/fee_calculator.py`: 统一 `FeeCalculator` —— USDT(quote)/SOL(base)可折算计价;
  其它资产(如 BNB)返回 `unpriced` 降级,**不再静默记 fee=0**; `FillFee`/`FeeResult` 承载
  `ZERO`/`PRICED`/`UNPRICED` 三态。
- `OrderFill` 新增 `fee_quote` + `fee_valuation_status` 两列, `_record_fills` 逐笔落真实手续费与计价状态。
- `_compute_fill_metrics` 返回 `(avg, fee_quote, fee_unpriced)`; `_ingest_fills` 检测到不可计价手续费 →
  告警 + `risk.pause` 降级(自动恢复, 不冻结)。
- **新增测试 10 条**, 全量 **531/531** 通过。存量库迁移见 runbook(order_fills 补两列)。

### P0-3 Ledger Reconstruction Engine(已完成)

- 新建 `at60_execution/ledger_reconstruction.py`: 从交易所真相(myTrades 全量成交)重建账务状态,
  链 `Exchange Truth → Trades → Orders → Buy Lots → Sell Allocations → Position → Cash → Ledger → Equity`。
- 幂等: 纯函数 `build_plan` 同输入同输出; `apply` 单事务「先清后插」可重复执行。
- dry-run(默认, 只产出计划)/ apply(无歧义才落库)。
- 守恒检查(base 守恒 / 无超卖 / 卖出分配覆盖 / 现金守恒)任一失败 → SAFE_MODE;
  歧义(成交历史不完整 / 缺现金锚 / 超卖 / 成交方向不一致 / 无交易所客户端)→ SAFE_MODE 拒绝 apply。
- 现金锚 `cash_before` 缺失即 SAFE_MODE(不猜绝对现金); 不可计价手续费不触发 SAFE_MODE 但权益置 None。
- **新增测试 12 条**, 全量 **543/543** 通过。

### P0-4 SELL Recovery(已完成)

- 消除 RECOVERY_REQUIRED SELL → 永久人工冻结: SELL 记账失败时 in-memory lot 已被
  `allocate_sell` 消费(内存先改、DB 事务整体回滚), 复用 `apply_recovered_fill` 会对已分歧
  内存二次消费; 故从 DB 开仓 `PositionLot`(权威未消费态)确定性重放 FIFO 分配。
- 新增 `ExecutionEngine.rebuild_sell_accounting`: 单事务落 `SellAllocation` + 减 lot +
  更新 `Position`(平均成本口径: 卖出不改 avg_price、清仓归零)+ `Order`(FILLED + RECOVERED),
  完成后 `_resync_sell_memory` 重同步内存持仓/FIFO lot 队列(覆盖分歧内存)。
- `order_recovery.py::_recover_accounting` 对 SELL 走 `_recover_sell_accounting`(本地成交数据
  缺失时从交易所真相补齐), 移除「SELL 保守冻结交人工」。
- **新增测试 7 条**(`test_v114_sell_recovery.py`)+ 更新 `test_v107_order_recovery.py` SELL 用例,
  全量 **550/550** 通过。无新表无迁移。

### P0-5 Reconciliation Matrix(已完成)

- 统一各对账器差异处置为单一判定点: 新建 `at60_execution/reconciliation_matrix.py`
  (`Severity` 四态 + `Finding`/`Verdict`/`ReconciliationMatrix`), 消除「各对账器分散、
  各自独立 arm kill」现状。
- 核心规则「单一对账器不得 kill」: 跨源资金级差异(equity_drift/orphan_trade/exchange_only/
  fill_truth_missing/fill_truth_mismatch)单源即 KILLED; 本地 DB 内部一致性破坏(fill_*/ledger_*/
  buy_lot/sell_alloc/lot_sum)单一对账器只 RECOVERY_REQUIRED(自愈不 kill), 需 ≥2 独立对账器
  同周期佐证才升级 KILLED; recover_unresolved → RECOVERY_REQUIRED; 数据不完整/现金异常 →
  DEGRADED; api_error/trade_duplicate/trade_id_gap 等 → PASS 仅记录。
- `run.py::_reconcile_loop` 重构为「摄入 findings → `matrix.verdict()` → `_apply_verdict`」,
  新增 `_apply_verdict`(PASS 无动作 / DEGRADED·RECOVERY_REQUIRED pause / KILLED arm+persist)。
- **新增测试 38 条**(`test_v115_reconciliation_matrix.py`), 全量 **588/588** 通过。无新表无迁移。

### P1-1 Backtest V2(已完成)

- 新建 `at80_backtest/backtest_robustness.py`: 用「鲁棒性评分」取代「单一收益」作为策略上线判据。
- 四维矩阵(5×6×5×5 = 750 格): 时间窗口(7/30/90/180/365 天)× 市场态(BULL/NORMAL/SIDEWAY/VOLATILE/
  BEAR/PANIC, `classify_regime` 由窗口数据分类)× 参数扰动(baseline/±5%/±10%)× 执行成本(0/5/10/20/30 bps)。
- 鲁棒性评分 `100 × 盈利占比 × (0.5×最坏稳健度 + 0.5×稳定性)`: 全亏 0 分「不可用」; 单次高收益不拉分;
  最坏格子深度亏损 / 跨格标准差大 → 降分。
- `RobustnessMatrixRunner` 注入 run_cell(单格异常不阻断整矩阵); `run_portfolio_cell` 接真实 `PortfolioBacktester`。
- **新增测试 21 条**(`test_v116_backtest_robustness.py`), 全量 **609/609** 通过。无新表无迁移。

### P1-2 Optimizer V2(已完成)

- 新建 `at80_backtest/backtest_optimizer.py`: 网格搜索 → Walk-Forward → 鲁棒性 → 风险调整排序,
  防「历史最优 ≠ 未来最优」过拟合。
- `build_grid` 展开参数笛卡尔积; `OptimizerV2` 注入 evaluate 回调(单点异常不阻断);
  `build_param_result` 从 train/test 收益序列派生均值/最坏/标准差/盈利占比/夏普/过拟合间隙/鲁棒性。
- 风险调整得分 = 鲁棒性 × max(0, 1+平均验证收益) × (1 - 0.5×clamp(过拟合间隙/10%)); 按此降序 `rank_params`。
- 复用 P1-1 的公共 `robustness_score`(跨窗口鲁棒性同口径)。
- **新增测试 15 条**(`test_v117_optimizer_v2.py`), 全量 **624/624** 通过。无新表无迁移。

### P1-3 System Lifecycle(已完成)

- 新建 `at50_risk/system_lifecycle.py`: 顶层生命周期状态机 `LifecycleState`(INIT/WARMING_UP/SYNCING/
  SELF_CHECK/READY/TRADING/DEGRADED/RECOVERY/SAFE_MODE/STOPPED), 与底层 `RiskStateMachine` 解耦互补。
- 迁移集中校验(`_move`): 非法/同态迁移拒绝; SAFE_MODE 任意可入(除 STOPPED)、仅 exit→READY; STOPPED 终态。
- **CanTrade 四维闸门**(`trading_gate` 纯函数): 生命周期态(READY/TRADING)+ 风险态(NORMAL)+ 连接 +
  对账共同决定; `reduce_gate` 允许降级/恢复期安全离场(REDUCE_ONLY 可减仓)。
- 迁移链: INIT→WARMING_UP→SYNCING→SELF_CHECK→READY⇄TRADING, DEGRADED→RECOVERY→READY。
- **新增测试 23 条**(`test_v118_system_lifecycle.py`), 全量 **647/647** 通过。无新表无迁移。

### P1-4 生产可观测性(已完成)

- 新建 `at60_execution/observability.py`: 统一采集执行延迟 / 对账漂移 / 恢复次数 / 订单失败率 /
  数据缺口 / 策略归因六类指标 + 阈值告警。
- `MetricsStore` 内存采集(计数器/仪表/延迟样本/策略 PnL); `order_failure_rate` / `percentile_rank`
  派生纯函数。
- `evaluate_alerts` 阈值告警: 订单失败率>5% CRITICAL; 对账漂移>2% / 数据缺口>300s / 延迟 P95>5000ms /
  连续恢复≥5 次 → WARNING; `AlertThresholds` 可覆盖; `strategy_attribution` 归因。
- **新增测试 17 条**(`test_v119_observability.py`), 全量 **664/664** 通过。无新表无迁移。

### P1-5 资金级 Circuit Breaker(已完成)

- 新建 `at50_risk/fund_circuit_breaker.py`: Equity / Position / Cash 三向漂移分级处置,
  取代「一漂移就冻结」的粗粒度做法 —— 小漂移先降级(只减仓)、逐级收紧到 PAUSE / KILL。
- 分级表(0.1%/0.2%/0.5%): Position/Cash 漂移首选 `REDUCE_ONLY`(减仓去险不冻结, 仅 >0.5% 才 PAUSE);
  Equity 漂移最严重 → 0.1% REDUCE_ONLY、0.2% PAUSE、0.5% KILL。
- `FundCircuitBreaker.assess` 三向独立分级后取最严重一档(NONE < REDUCE_ONLY < PAUSE < KILL),
  返回 `BreakerDecision`(action + 各维度 + reason); `classify_drift` 纯函数可独立测试。
- **新增测试 15 条**(`test_v120_circuit_breaker.py`), 全量 **679/679** 通过。无新表无迁移。

## V11.0 深度审计 — 13 项资金正确性缺陷修复(2026-09-08)

**定位: 不再加功能, 逐行审查执行/风控/账务/行情链路, 证明「交易所/网络/进程/DB 异常下不错误改账」。**

| # | 领域 | 缺陷(简) |
|---|------|----------|
| F1 | 账务 | 已清 lot 未归零 → 交叉对账假急停 |
| F2 | 账务 | 买入费未进均价成本账 |
| F3 | 恢复 | 终态前部分成交静默丢弃 |
| F4 | 恢复 | 恢复路径记账/状态分步提交, 有重复/漏记账窗口 |
| F5 | 恢复 | 启动自愈不重放记账 |
| F6 | 执行 | 落库失败仍投交易所 |
| F7 | 部署 | init.sql 双表结构来源 + MariaDB 语法 |
| F8 | 风控 | 急停持久化静默吞错 |
| F9 | 风控 | 核心仓 REDUCE 未过 can_sell |
| F10 | 行情 | myTrades 分页缺失 |
| F11 | 恢复 | 恢复缺真实手续费 |
| F12 | 账务 | lot 无唯一约束无幂等 |
| F13 | 行情 | WS raw/agg 成交 ID 命名空间冲突 |

**新增测试 8 条**(F3/F4/F5/F8/F9/F10/F11/F12/F13 回归), 全量 **512/512** 通过。
**无新增表**; 存量库迁移见 runbook(F12: `position_lots.client_order_id` 唯一索引)。
修改 `models.py` / `market_rest_client.py`(`get_my_trades_all` 分页)/ `market_ws_client.py`(`@aggTrade`) /
`execution_executor.py` / `order_recovery.py` / `startup_reconciler.py` / `exchange_truth_reconciler.py` / `risk_lot.py`。

详见 `cc_task_v11.md`「深度审计修复记录(F1-F13)」。

## V11.0 规划 — Production Readiness(方向, 深度审计已完成, 2026-09-08)

> 外部评审(基于 commit b2dd5e2)结论: 工程完整度约 **84/100**, 实盘准备度约 **70/100**。
> 核心判断: 已从「策略原型」进入「接近实盘基础设施」阶段; 下一步「质量 > 数量」——
> 不再增加策略, 而是证明系统在交易所/网络/进程/DB/Redis/WS/订单状态异常下不会错误修改资金账本。

**P0(执行可靠性)**
- P0-1 Exchange Truth V2: myTrades 分页(fromId/startTime/endTime), 消除 limit=100 窗口假设; 恢复链路补真实手续费(commission/commissionAsset)。
- P0-2 Ledger Reconstruction: 由 Exchange Truth + Order + Fill 重建 PositionLot/SellAllocation/Position。
- P0-3 SELL Recovery: 消除 RECOVERY_REQUIRED SELL → 人工处理(FIFO 分配信息丢失)。
- P0-4 Reconciliation Engine V2: 统一 Order/Fill/Position/Lot/Cash/Equity/Ledger 的 Truth Reconciliation Matrix。

**P1(回测/风控/可观测性)**
- P1-1 Backtest V2: 30/90/180/365 天 + Walk-Forward + OOS + Monte Carlo + 参数扰动 + 滑点/手续费压力。
- P1-2 Optimizer V2: 网格搜索 → Walk-Forward + Robustness + 风险调整排序(防过拟合)。
- P1-3 System Lifecycle: INIT/WARMING_UP/SYNCING/SELF_CHECK/READY/TRADING/DEGRADED/RECOVERY/SAFE_MODE/STOPPED 顶层状态机。
- P1-4 生产可观测性: 执行延迟/对账漂移/恢复次数/订单失败率/数据缺口/策略归因 + 告警。
- P1-5 资金级 Circuit Breaker: Equity/Position/Cash 三向漂移分级处置。

**P2**: 执行/对账/策略三块 Dashboard; AI 权限架构级隔离。详见 `cc_task_v11.md`。

## V10.7 — 恢复 + 混沌工程: 7 项 P0/P1(订单事件日志 / 事件信封幂等 / 订单恢复 / 交易所真相 / RECOVERY_CHECK / 不变量 / Chaos)(2026-09-08)

**定位: 外部评审收尾第二阶段 —— 把崩溃恢复、交易所真相、急停解除、异常注入收敛补齐, 达成生产级自愈闭环。**

| 交付 | 内容 |
|------|------|
| P0-a 订单事件日志 | `execution_events` 表(append-only, event_id 非空唯一)+ `ExecutionEventLogger`, 订单生命周期全事件可追溯 |
| P0-b 事件信封幂等 | `bus.py` 事件信封(event_id/event_time/event_version/source)+ 有界内存去重, 消费不重不丢 |
| P0-c 订单恢复引擎 | `order_recovery.py`: UNKNOWN/SUBMITTING 周期收敛(交易所真相)+ RECOVERY_REQUIRED BUY 账务重建(进程内补镜像 / 重启后完整记账), SELL 保守冻结 |
| P0-d 交易所真相对账 | `exchange_truth_reconciler.py`: 本地 filled_quantity vs myTrades 成交额对账(fill_truth_missing/mismatch/orphan_trade) |
| P1-e RECOVERY_CHECK | 风险状态机新增 RECOVERY_CHECK: KILLED → reset → RECOVERY_CHECK(仍不可交易)→ confirm_recovered → NORMAL, 禁止裸 reset |
| P1-f 10 不变量 | `test_v107_invariants.py`: 幂等/持仓守恒/lot 守恒/账本守恒/记账原子性/成交覆盖/FIFO 盈亏/REDUCE_ONLY/急停持久化 |
| P1-g Chaos 测试 | `test_v107_chaos.py`: 超时/重复成交/部分成交/DB 回滚/未知订单 故障注入 + 自愈收敛断言 |

**新增表**: `execution_events`(全库 24 → 25 张)。
**新增模块**: `execution_events.py` / `order_recovery.py` / `exchange_truth_reconciler.py`。
**新测试**: test_v107_execution_events / test_v107_event_envelope / test_v107_order_recovery /
test_v107_exchange_truth / test_v107_recovery_check / test_v107_invariants / test_v107_chaos。
**验证**: 497/497 测试全绿。

**修复: 交叉对账实盘 ledger_missing 误报** —— `AccountLedger` 仅纸面模式落库(实盘
`cash_before=None` 不写账本), 但 `CrossReconciler.reconcile()` 此前只查实盘订单并核对 base
资产账本行, 导致每笔实盘成交被误判 `ledger_missing` → 误触急停冻结。修复: `reconcile()` 改为
核对实盘+纸面订单, `ledger_position` 维度仅对纸面订单核对(实盘 SOL 持仓一致性由 buy_lot/sell_alloc
+ lot 总和对账 + 权益对账兜底); 新增回归 `test_live_order_without_ledger_not_flagged` 与纸面
四维自洽测试。

## V10.6 — 生产加固: 7 项 P0/P1(ACK 语义 / 强一致记账 / 记账锁 / 幂等键 / 数量分离 / ExchangeInfo 禁 BUY / REDUCE_ONLY)(2026-09-08)

**定位: 外部评审收尾 —— 把成交后记账的强一致、幂等去重、方向闸门补齐, 消除最后几处「异常下静默漂移 / 重复摄入 / 规则未知开仓」的风险。**

| 交付 | 内容 |
|------|------|
| P0-a ACK 语义 | `bus.py` ACK 作为业务成功结果; 转投失败留 PEL + `recover_pending` 兜底(不丢不重) |
| P0-b 强一致记账 | 成交后 Position / PositionLot / SellAllocation / AccountLedger 四表单事务提交; 失败整体回滚 + 置 `orders.accounting_state=RECOVERY_REQUIRED` + 急停冻结 |
| P0-c 记账锁 | Position/Lot 记账 symbol 级 `asyncio.Lock`, 串行化核心仓并发成交的 add_buy / allocate_sell 竞态 |
| P0-d 幂等键 | `order_fills.fill_idempotency_key`(非空唯一, `订单ID:成交ID`, 缺失成交ID落 `na`), 修复原双可空唯一键的 NULL 漏洞 |
| P1-e 数量分离 | `signal.quantity` 不再被执行引擎原地改写; `exec_qty` 独立承载 REDUCE_ONLY 缩量 / 交易规则过滤调整; `signals` 落原始意图、`orders` 落实际提交量 |
| P1-f 禁 BUY | 实盘 exchangeInfo 拉取失败时 BUY 本地拒绝(不下单), SELL 减仓放行; 失败不缓存、下次自动重试 |
| P1-g REDUCE_ONLY 态 | 风险状态机新增 REDUCE_ONLY(禁开新仓/保留卖出)+ `can_buy`/`can_sell`; `RiskManager.check()`/`_on_signal`/核心仓 ADD 按方向分流 |

**新增列**: `orders.accounting_state`(VARCHAR(20) NOT NULL DEFAULT 'OK')、`order_fills.fill_idempotency_key`(VARCHAR(128) NOT NULL UNIQUE); 存量库需 ALTER + 回填(见 runbook)。
**无新增表**(全库仍 24 张); 新测试 test_v106_accounting_tx / test_v106_accounting_lock /
test_v106_fill_idempotency / test_v106_signal_exec_qty / test_v106_exchange_info_block /
test_v106_risk_reduce_only(共 22 条), P0-a 扩展 test_v105_eventbus_dlq。
**验证**: 447/447 测试全绿。

## V10.5 — 一致性加固: 5 个 P1(EventBus DLQ / ExchangeInfo / WS 回补 / REDUCE_ONLY / 风险状态机)(2026-09-07)

**定位: 停止加策略, 修交易系统最后 20% —— Order→Fill→Ledger→Position 链在异常下的自洽。**

| 交付 | 内容 |
|------|------|
| EventBus DLQ | `bus.py` 消费处理失败先重试 3 次(重入同流), 仍失败转 `<stream>:dlq` 死信队列, 不再静默丢弃; `dead_letter_count()` 可观测积压 |
| ExchangeInfo 过滤 | `exchange_filters.py` + `get_exchange_info` 按 LOT_SIZE/PRICE_FILTER/MIN_NOTIONAL 对齐 stepSize/tickSize/minQty/minNotional, 违规本地拒绝(不投交易所); 拉取失败自动降级不过滤 |
| WS 断线回补 | `market_engine.resync()` + `merge_klines/merge_trades` 幂等合并 REST 重拉快照, 刷新数据校验基线; `on_reconnect` 接线 |
| REDUCE_ONLY | 现货卖出执行前重读持仓封顶(无持仓拒绝/超仓缩量), 关掉风控审批→执行竞态; `orders.reduce_only` 标记(仅 SELL 为 1) |
| 风险状态机 | `risk_state.py` 显式 NORMAL/PAUSED/KILLED 三态, 取代隐式时间阈值暂停; 同因续期不重复告警, 进入 PAUSED 落 `risk_events(event_type='risk_state')` 审计 |

**新增列**: orders.reduce_only(存量库需 `ALTER TABLE orders ADD COLUMN reduce_only BOOLEAN DEFAULT 0`)。
**无新增表**; 新测试 test_v105_eventbus_dlq / test_v105_exchange_filters / test_v105_ws_gap_recovery /
test_v105_reduce_only / test_v105_risk_state。
**验证**: 423/423 测试全绿。

## V10.4 — 三维交叉对账(Order / Fill / Ledger / Lot 一致性)(2026-09-07)

**定位: 把「订单→成交→账本→批次」四个独立写入环节的漂移变成可检测、可冻结的硬约束。**

| 交付 | 内容 |
|------|------|
| CrossReconciler | `at60_execution/cross_reconciler.py`: 逐笔核对同一 client_order_id 在 Order / OrderFill / AccountLedger / PositionLot+SellAllocation 四维是否自洽 |
| 五类检查 | fill_coverage / fill_side / ledger_position / buy_lot / sell_alloc; 任一漂移 → 急停冻结(KillSwitch.arm + persist) |
| 接线 | run.py `_reconcile_loop` 实盘分支, 纯 DB 读(不查交易所), 窗口过滤(默认 900s / 上限 200 单) |

**无新表无迁移**; 新测试 test_v104_cross_reconcile(约 10 条)。

## V10.3 — FIFO Lot 会计(PositionLot / SellAllocation)(2026-09-07)

**定位: 在平均成本口径之上加一层 FIFO 审计, 精确逐批已实现盈亏。**

| 交付 | 内容 |
|------|------|
| Lot 会计 | `PositionLot` + `SellAllocation` + `LotTracker`: FIFO 精确已实现盈亏 + 剩余成本(不动平均成本口径); 买入费摊入 lot 成本、卖出费一次性扣 |
| 账本回写 | AccountLedger 落 realized_pnl / matched_cost |
| 对账不变量 | 开仓 lot 总和 == 持仓量; 全平仓 FIFO 累计 realized == 平均成本累计 realized; 崩溃恢复 FIFO 队列持久化 |

**新增表**: position_lots / sell_allocations; account_ledger 加 realized_pnl / matched_cost 两列(存量库需 ALTER)。
新测试 test_v103_lot_accounting。

## V10.1 / V10.2 — 生产硬化: 订单→成交→账本链 + 对账盲区(2026-09-07)

**定位: 补齐订单生命周期与对账的最后一公里 —— 状态迁移合法性、幂等摄入、反向对账。**

| 交付 | 内容 |
|------|------|
| Order→Fill→Ledger 链 | UNKNOWN/SUBMITTING 状态迁移合法性; 异常分类(4xx→REJECTED, 5xx/网络→UNKNOWN); OrderIntent DB 幂等唯一键; OrderFill 逐笔落库+幂等摄入 |
| 手续费合成 | fee_quote 合成 + AccountLedger 落 commission |
| 对账盲区 | `_execute_live` 下单前落 SUBMITTING + 每次尝试落 ExecutionAttempt(attempt_no/outcome); reconcile_live 反向检出 EXCHANGE_ONLY; startup_reconciler 对 SUBMITTING 按 clientOrderId 反查收敛 |

新测试 test_v101_order_fill_ledger / test_v102_reconciliation_execution。

## V10.0 — 实盘安全三件套(启动对账 / 权益对账 / 急停端点)(2026-09-07)

**定位: 把 V8 的「安全闸门」从「能告警」补成「能冻结 + 能恢复」——为无人值守实盘补齐最后一道硬保护。**
原则: 不新增依赖、不碰主交易链路; 冻结必须是**持久化的、人工才解除的**急停(区别于 CircuitBreaker 的 cooldown 自动复位)。

| 交付 | 内容 |
|------|------|
| 急停开关 KillSwitch | `at50_risk/risk_killswitch.py` + `kill_switch_state` 表(单行 id=1); `arm()` 幂等、`disarm()` 人工解除、`persist()`/`load_from_db()` 持久化, **重启后仍冻结** |
| 统一闸门接入 | `RiskManager.can_trade()` 首查 `kill_switch.is_armed`(优先于熔断/异常保护); `block_reason`/`status` 补急停字段 |
| 启动对账 StartupReconciler | 实盘启动时拉交易所挂单+成交历史, 对崩溃窗口做**确定性自愈**(交易所已 FILLED → 本地改 FILLED + 状态机推进); 歧义(无交易所订单ID/孤儿挂单/无法匹配)记入未解决差异 → 急停冻结 |
| 权益对账 reconcile_account | 本地权益 vs 交易所权益(计价资产 + base 资产×last_price), 超容差(默认 2%)→ 持久急停(非 60s pause) |
| exchange_order_id 落库 | `_execute_live`/`_execute_paper` 改为 5 元组返回, `_update_order_status` 落 `exchange_order_id`(启动对账可匹配) |
| 急停撤单 | `ExecutionEngine.cancel_all_open_orders(symbol)`: live 撤交易所挂单 / paper 撤本地 NEW 单, 落库 CANCELED + 状态机回退 |
| 急停/恢复端点 | `POST /api/emergency/kill`(冻结+撤单+持久化+记事件)、`POST /api/emergency/recover`(解除+持久化+记事件) |
| 配置 | `startup_reconcile_enabled` / `equity_reconcile_tolerance_pct`; run.py 启动接线(实盘才对账)+ `_reconcile_loop` 周期权益对账 |

**新增表**: kill_switch_state(全库 18 → 19 张)。
**新增配置**: startup_reconcile_enabled / equity_reconcile_tolerance_pct。
**验证**: 340/340 测试(313 → +27); 新增 test_v10_killswitch / test_v10_reconciliation / test_v10_emergency_api。

**冻结不变**: 零新依赖; 纸面模式不查交易所(启动对账仅实盘); 急停不自动复位, 只能人工 recover。

## V9.0 — SOL Adaptive Swing Trader: 记忆交易实验平台 M1(2026-09-07)

**定位: 不是加策略, 而是把系统升级为「有记忆的交易实验平台」——沉淀每次判断/交易/环境/盈亏原因, 供 AI 未来 6-12 个月优化。**

原则: Binance 单所 + SOLUSDT 单币 + 双仓 + 低频; AI 只优化不交易; 每步可回滚、兼容 paper、加测试加日志。

| 交付 | 内容 |
|------|------|
| Portfolio Manager | `at40_portfolio/` 薄编排层: 核心/交易/现金三桶(config 驱动, 替代硬编码 70/30) |
| Core Position Manager | ADD/REDUCE/HOLD + Trend Break Protection(EMA 死叉 / BTC 锚失败 / PANIC) |
| Trading Journal | `trade_records` 表: 成交闭环 entry/exit/profit/holding/max_profit/max_drawdown |
| Strategy Version | `strategy_versions` 表: 参数快照(不可变, 供回测-实盘对比) |
| 统一闸门 | `RiskManager.can_trade()` 合并熔断/异常保护, `_on_signal` 与核心仓决策短路 |
| 每日复盘 | `reports/YYYY-MM-DD.md` 自动生成(决策/成交/绩效) |

**新增表**: trade_records / strategy_versions(全库 15 → 17 张); position_bucket 追加 target 三列。
**新增配置**: portfolio_core/trading/cash_ratio、portfolio_rebalance_interval_seconds、daily_report_enabled 等。
**验证**: 235/235 测试(单元 215 + 集成 20); 新增 test_v9_portfolio / test_v9_journal / test_v9_strategy_version。

## V9.0 — M2: Regime 6 态 + 策略整合 + 回测指标 + at85_optimizer(2026-09-07)

**前置目的: 让 at85_optimizer 能跑起来 —— 可量化回测指标作目标函数、strategy_versions 作实验台账、统一策略分组作优化单位。**

| 交付 | 内容 |
|------|------|
| Regime 6 态 | 中性区三档: NORMAL <1.0% / SIDEWAY 1.0~1.5% / VOLATILE ≥1.5%; BULL/BEAR/PANIC 判定不变, 6 张系数表补键 |
| 策略整合(薄分组层) | `strategy_group.py` 3 伞: Trend Swing(trend+entry) / Mean Reversion(grid+entry) / Exit Manager(exit); 归因统一到伞名 |
| Exit Manager 统一 | 分批止盈阶梯 settings 化(`sell_take_profit_ladder`) |
| 回测指标 6 项 | win_rate / profit_factor / holding / sortino / calmar / attribution; 闭环成交跟踪 |
| AI 优化器 | `at85_optimizer/`: 候选生成 → 回测评估 → 落库 → 排序提案(**不自动 activate**) |

**验证**: 280/280 测试(M1 235 → +45); 新增 test_v9_regime_6state / test_v9_strategy_group /
test_v9_backtest_metrics / test_v9_optimizer。

**修复已知不一致**: `trade_records` 与 `strategy_performance` 两表归因首次统一(此前一记 source_strategy、
一记 "decision")。

## V9.0 — M3: 审查落地(README 对齐 + 账本审计 + 条件滑点 + HMM/Funding 可选模块)(2026-09-07)

**前置: 外部评审(7.6/10)逐条对照后, 大部分建议已落地; 本里程碑补齐 5 项真正未落地且有价值者。**

| 交付 | 内容 |
|------|------|
| README 对齐 V9.0 | 修 "V2.0" 冻结标题、目录表补 at70_journal/at40_portfolio/at85_optimizer、6 态 Regime + 3 策略伞、回测 6 指标、测试数 303、`cc_task.md` → `cc_task_v9.md` |
| account_ledger 审计账本 | `account_ledger` 表 + `AccountLedgerWriter`: 每笔成交落 USDT + SOL 两行 before/change/after, 落库失败降级不打断成交 |
| Regime 条件滑点 | `SlippageModel(regime_bps)` PANIC/VOLATILE/BEAR 放大; intent 透传 regime; `slippage_regime_bps` settings |
| HMM Regime(可选) | `regime_hmm.py` 纯 Python 对角协方差高斯 HMM(log 域前向-后向 + Viterbi); `regime_hmm_train.py` 离线训练; 默认关闭不接实盘 |
| Funding + OI 情绪(可选) | `market_futures_client.py` + `sentiment.py` 合成情绪分; 默认关闭, run.py 低频轮询挂起 |

**新增表**: account_ledger(全库 17 → 18 张)。
**新增配置**: slippage_regime_bps / regime_hmm_enabled / regime_hmm_model_path / sentiment_enabled / sentiment_poll_interval_seconds / sentiment_funding_threshold / binance_futures_base_url。
**验证**: 303/303 测试(M2 280 → +23); 新增 test_v9_account_ledger / test_v9_regime_slippage / test_v9_regime_hmm / test_v9_sentiment。

**冻结不变**: HMM / Funding-OI 默认关闭不接实盘 regime; 零新依赖(纯 Python); 单所单币双仓低频。

## V9.0 — AI 供应商化(2026-09-07)

**前置: 复用 bianAgent `src/config/llm_config.py` 的供应商选择模式, 将 AI 顾问改造为供应商可切换、Key 全部入 .env。**

| 交付 | 内容 |
|------|------|
| AI 供应商配置中心 | `at30_strategy/llm_config.py`: `LLMConfig(BaseSettings)` + `get_llm_config()` + `resolve_provider()`, 支持 openai/qwen/deepseek 三供应商, Key 从 `.env` 读、不硬编码 |
| AIAdvisor 供应商化 | `strategy_ai_advisor.py` 改用 `resolve_provider`, 统一走 OpenAI Chat Completions 兼容协议 |
| 配置 | `ai_provider` 作供应商选择参数(默认 deepseek); `ai_base_url`/`ai_api_key` 改为通用覆盖(空则用供应商默认); `.env` 迁移 QWEN/DEEPSEEK/OPENAI Key |

**验证**: 313/313 测试通过; 新增 `test_v9_ai_provider`(供应商解析/未知禁用/Key 缺失禁用)。

**冻结不变**: 零新依赖(不引入 langchain, 复用 aiohttp 原生协议); AI 只建议不交易。

## V8.0 — 生产加固与账务修复(2026-09-07)

**原则: 单一记账、状态可持久化、重启可对账、主网有安全闸门。**

针对 `at60_execution/*` / `run.py` / `database/persistence` 的审查, 修复 13 项问题
(4 P0 + 4 P1 + 5 P2):

| 层级 | 要点 |
|------|------|
| P0 账务 | 消除成交双重记账(Bucket 变纯拆分跟踪, 总账只经 PortfolioEngine); PortfolioEngine 正确接线; 状态机死代码打通; PARTIALLY_FILLED 处理 |
| P1 持久化 | 交易状态机 + PaperBroker 现金落库; 交易所持仓对账(PositionReconciler); 行情校验(MarketDataValidator) |
| P2 安全 | AI 降频 86400s + 参数审批层; Kill Switch 补快速崩盘/余额不匹配; 日志轮转; 主网实盘二次确认守卫 |

**顺带修复**: 回测卡死(回测未建表 → `init_db()` 兜底)+ 补装 `aiosqlite` 测试依赖。

**验证**: 219/219 测试(单元 199 + 集成 20)。新增模块 data_validator / reconciliation / ai_parameter_guard;
新增表 trade_state / paper_state; 新增配置 live_trading_confirm / reconcile_interval_seconds。

## 状态总览(按评估框架)

```
① Accounting      ✅ V6(账本+对账不变量)+ V8(单一记账固化)
② Backtest真实化   ✅ V7(真实策略管线+滑点+次bar)
③ Test/Invariant  ✅ 219 用例四层
④ No Lookahead    ✅ V7(次bar执行)
⑤ Attribution     ← P1 下一项
⑥ Walk Forward    ← P1
⑦ AI 自动调参      ✅ V8(审批层已落地, 待 A/B 验证)
```

**结论: 暂不建议实盘** —— 策略跑输持有(V7 结论), 但 V8 已补齐无人值守所需的
正确性/持久化/对账/安全闸门, 纸面可安全长跑。

## V7.0 — Backtest Realism 回测真实化(2026-09-07, commit 2eba4f7)

**原则: 回测里的策略, 必须就是实盘里的策略。**

V6 审查发现的结构性问题(修了表面、留下深层问题)并全部修复:

| 问题 | 修复 |
|------|------|
| 回测内嵌 "+3%/-3%/15%" 隐藏交易策略 | 删除, 回测驱动真实 StrategyEngine |
| Decision 直接决定交易数量 | 降级为建议参考值, 数量由 Sizer 决定 |
| unknown strategy 静默 0.5 权重 | 警告并跳过计票 |
| 无滑点模型 | SlippageModel(0/5/10/20bps 敏感性) |
| candle close 同时判断+成交(look-ahead) | NextBarExecutor 次bar执行 |
| BTC 精确 timestamp match | AsOfJoiner asof 对齐+数据龄 |
| interval 分页浪费 | timeframe.py 统一换算 |

**验证**: 217/217 测试; 真实 SOL 滑点敏感性:
0bps +0.18% / 10bps +0.01% / 20bps -0.21%

**关键洞察(诚实结论)**: 48+ 笔交易在 10bps 滑点下吃掉全部利润——
高频网格摩擦成本是最大亏损源; 交易仓在上涨段被网格卖出无法吃到趋势。
P1 参数优化(网格频率/Attribution)有了量化目标。

## V6.0 — Correctness First(2026-09-07, commit 9f76b03)

**原则: 不新增功能, 修复正确性。** 代码审查确认 8 个真实 Bug 并全部修复:

| Bug | 影响 |
|-----|------|
| 策略名 buy/sell ≠ 权重键 entry/exit | entry=1.0/exit=1.3 权重失效, 掉 0.5 默认值 |
| 融合信号 strategy='buy+decision' | on_fill 回调静默丢失 |
| 回测 trade PnL 用 core_cost | 交易仓盈利严重虚高(170-100 vs 170-150) |
| 回测交易仓初始化为 pass | 双仓模型从未完整运行 |
| 回测风险因子固定 btc=0/neutral/drawdown=0 | Live ≠ Backtest 风险模型 |
| 历史数据 1000 根上限 | "7天回测"实际只有 16.7 小时 |
| 回测自写简化 Regime / Sharpe 固定 1m 年化 | 数据可信度问题 |

**交付**: StrategyType 枚举统一 / Signal.source_strategy 回调路由 /
PortfolioLedger(双仓独立成本+对账 reconcile API)/ 交易仓真实生命周期 /
分页历史数据 / BTC 对齐风险因子 / 共用 MarketRegimeEngine /
金融正确性测试四层(Invariant/Accounting/Regression)

**验证**: 202/202 测试; 回测对账恒平衡; 真实 3 天 4320 根 BTC 对齐:
core +147.18 / trade -27.32 / 17 次再平衡 / 37 笔

**诚实结论**: 修正账目后, 3 天策略跑输 Buy-Hold 1.57%(此前账目虚高)。
交易仓高抛低吸在上涨段提前卖出是主要亏损源 —— 这是 P1 参数优化
(阈值敏感性/止盈阶梯)的真实起点, 而非继续堆功能。

## V3.0 — 交易决策层与信号闭环(2026-09-06, commit 062f7fa)

**目标**: 从"能运行的交易机器人"升级为"可长期迭代的量化交易平台"

### 交付
| 模块 | 内容 |
|------|------|
| Decision Engine | 多策略加权融合 → 唯一 BUY/SELL/HOLD(exit 1.3 保命权重, regime 系数, 高分退出优先) |
| Portfolio Engine | 成本管理: 卖出降本/保本价/成本曲线/目标仓位/再平衡 |
| signal_result | 信号未来收益逐分钟跟踪(1h 窗口), 供调权与 AI 学习 |
| Walk-Forward 回测 | 滚动窗口+阈值网格调优+过拟合间隙检测 |
| 交易/订单状态机 | IDLE→ENTRY_PENDING→HOLDING→EXIT_PENDING→CLOSED, 防重复建仓 |
| Exit 标签 | profit_target / overbought / trend_reverse / risk_reduce |
| Alpha Engine | 综合评分(5 因子, 含 SOL/BTC 相对强弱) |
| database 惰性引擎 | reset_engine 支持测试隔离 |

### 验证
- 测试 **140/140**(新增 31 个 V3 测试)
- E2E(真实 SOL 行情): 融合决策 BUY(net 0.42)→成交→EXIT_PENDING→CLOSED→IDLE 零非法迁移;
  signal_result 实时落库(grid+decision BUY profit 追踪中)
- 修复: enum 类内 dict 被成员化(迁移表外置)、状态机推进时序(移到持仓更新后)、
  纸面模式中间态补齐、Portfolio 变量名、V2 幂等测试语义更新(HOLDING 拦截重复买入为正确行为)

## V2.0 — SOL 专业量化系统(2026-09-06, commit 505010f)

**目标**: 评分化策略 + 市场环境 + 百分比风控

### 交付
- Entry 评分模型(5 维加权, ≥80 买 / 60-80 观察 / <60 禁止)
- Exit 分批止盈(5%→20%/10%→30%/20%→50%) + 移动止盈(5%) + 趋势退出
- Market Regime Engine(BULL/SIDEWAY/BEAR/PANIC)
- 百分比风控(仓位 40%/单笔 5%/日亏 5%/回撤 15%) + 异常保护
- 幂等执行 + strategy_performance + 持仓快照
- 回测引擎 v1 / Redis Stream 事件总线 / Dashboard 增强
- 标准信号: score 0~100 + reason 列表 + indicators 快照落库

### 验证
- 测试 109/109; E2E: Entry 65 分正确进入观察档被拦截; 网格+Exit 全链路

## 结构重组(commit f87e5e9)

- 目录重命名 atXX 前缀(at01_common ~ at90_deploy), 拼写修正(ayalytics→analytics, startegy→strategy)
- 全部包扁平化: **目录即包名**, 模块文件带前缀(market_engine.py / strategy_base.py ...)
- at90_web 分层: web_app / web_api_routes / web_ws_stream / web_state / web_serve_standalone(前端独立启动)
- 39 文件全局 import 重写, git rename 历史保留

## V1.0 — 全链路实现(2026-09-06, commit 5029ca0)

- 行情(WS 组合流+自动重连+预热)→分析(VWAP/CVD/Whale/吸筹)→策略→风控→执行(纸面)→Web
- 7 张表 ORM + Docker 部署(MySQL/Redis/compose) + 监控面板
- 测试 75/75; 真实币安测试网 BTC 端到端验证

## 待办(P2, V11.3 起冻结)

> V11.3 冻结产品定义后, 以下原 P2 项与「不新增策略/币种/合约/高频/LLM 下单」冲突, 全部冻结不做:

- [ ] ~~AI 参数建议自动应用到策略~~ → 冻结: AI 只优化不直接下单(提案需人工 activate)
- [ ] ~~多币种并行~~ → 冻结: 单币 SOLUSDT
- [ ] ~~Funding Rate 情绪因子(需合约 API)~~ → 冻结: 不增加 Futures(情绪因子已有, 默认关)
- [ ] ~~链上大额转账监控(交易所流入/流出)~~ → 冻结: 新外部数据源
- [ ] ~~策略市场化(参数云端配置热加载)~~ → 冻结: 单机无人值守
