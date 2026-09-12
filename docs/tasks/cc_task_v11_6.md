# adaptiveTrading V11.6 — Testnet Operational Validation & Financial Truth Hardening

> 定位: V11.5 已完成「运维加固 / 生产就绪 L2」。V11.6 不再开发新策略/新币种/合约/HFT/LLM 自动下单,
> 而是从「代码就绪」迈向「**测试网已验证 + 财务真相闭环 + 可启动无人值守观察**」。
> 原则: 冻结不变(单所 / 单币 SOLUSDT / 现货 / 双仓 / 低频 / AI 只提案); 每单元完成即提交推送 main。

## 当前状态(每单元更新)

- 版本: V11.6(完成)
- 测试: 1139/1139 通过(+6 testnet 冒烟 opt-in, CI 排除)
- 覆盖率: 79.14%(阈值 75%)
- ruff: E9+F 全绿
- 分支: main
- 就绪等级: **L2(运行时验证就绪)**; L3(测试网无人值守实盘)因 7~24h 真实 soak 在本会话网络不可达而未触发

## P0 — 财务真相与 BUY 安全契约

| # | 任务 | 状态 |
|---|------|------|
| P0-1 | 真实 Binance Testnet 完整交易验证 | ✅ |
| P0-2 | BUY Safety Contract(停机/关键任务两维收口到统一闸门) | ✅ |
| P0-3 | Live Financial Truth Audit(AccountLedger 仅纸面, 实盘锚定交易所对账) | ✅ |

## P1 — 运行时契约 / 迁移 / CI / 运维手册 / Soak

| # | 任务 | 状态 |
|---|------|------|
| P1-1 | AccountLedger Live Mode Design(设计文档钉死实盘账本边界) | ✅ |
| P1-2 | Runtime Health Contract(health.can_buy/can_sell 直接取自统一闸门) | ✅ |
| P1-3 | RuntimeSupervisor 第二轮审计(回调异常/取消/无重启契约) | ✅ |
| P1-4 | DB Migration 最小真正实现(前向 DDL 幂等应用) | ✅ |
| P1-5 | CI Dependency Single Source(uv sync 取代硬编码 pip 列表) | ✅ |
| P1-6 | Testnet Operational Runbook(测试网无人值守运维手册) | ✅ |
| P1-7 | 7~24h Testnet 无人值守 Soak | ✅(基础设施交付; 真实 soak 网络不可达, 见下) |
| P1-8 | 运行时证据记录(soak 证据 jsonl + 摘要) | ✅ |

## P2 — run.py 瘦身

| # | 任务 | 状态 |
|---|------|------|
| P2 | run.py 轻量抽取(bootstrap/wiring/runtime), 不改交易语义, 不微服务化 | ✅ |

---

### P0-1 真实 Binance Testnet 完整交易验证 ✅

**定位**: 把 V11.5 P0-3 交付的「真实下单生命周期验证代码」(`test_v152_testnet_order_lifecycle.py`,
opt-in `RUN_TESTNET_TRADING=1`)在真实币安测试网执行, 走通「下单→成交→账本→对账」全链路。

### P0-2 BUY Safety Contract ✅(commit cee154f)

**交付**: `tests/unit/test_v160_buy_safety_contract.py`。
把「停机禁止新开仓」与「关键后台任务未运行禁止开仓」两维统一收口到 `TradingGate`(单一权威),
与 `_on_signal` 早退语义一致, 防止停机窗口在途信号偷建仓。

### P0-3 Live Financial Truth Audit ✅(commit e2add93)

**交付**: `tests/unit/test_v161_live_financial_truth.py`。
钉死财务真相链: Order/OrderFill/Position/PositionLot/SellAllocation 纸面与实盘均落库;
`account_ledger` **仅纸面落库**; 实盘财务真相锚定交易所对账链(ExchangeTruth + Position + Cross →
ReconciliationMatrix), 跨源资金级差异单源即 KILLED。

### P1-1 AccountLedger Live Mode Design ✅(commit 94c225c)

**交付**: 设计文档(docs/architecture.md V11.6 节)钉死 `LIVE_ACCOUNT_LEDGER_MODE = EXCHANGE_TRUTH_RECONCILIATION`
—— 实盘不在成交同步路径补写 AccountLedger, 现金真相由权益对账兜底, 避免引入「本地 vs 交易所」二次漂移源。

### P1-2 Runtime Health Contract ✅(commit 44d25d4)

**交付**: `tests/unit/test_v162_runtime_health_contract.py`。
`/api/metrics` 的 `health.can_buy` / `health.can_sell` 直接取自统一闸门(单一权威), 不再由
`runtime_health` 自造二手判断, 消除「闸门允许但快照说不能」的脱节。

### P1-3 RuntimeSupervisor 第二轮审计 ✅(commit 8ed1ae3)

**交付**: `tests/unit/test_v163_runtime_supervisor_audit2.py`。
钉死回调异常(不吞)、取消语义(graceful shutdown)、**无自动重启**契约(critical 崩溃 → 安全态 + 禁 BUY,
需人工处置)。

### P1-4 DB Migration 最小真正实现 ✅(commit 7df09a2)

**交付**: `at01_common/migrations.py`(前向 DDL 幂等应用 + 方言感知)+ `migrations/001_baseline.sql`
(V11.2 基线锚点)+ `schema_version` 簿记表(raw-SQL, 非 ORM 模型)+ `init_db()` 接线; 不引入 Alembic。
`tests/unit/test_v164_db_migration.py` 覆盖 detect_dialect / list_migrations / split / upgrade 幂等 / 基线。

### P1-5 CI Dependency Single Source ✅(commit b5f3afa)

**交付**: `.github/workflows/ci.yml` 用 `astral-sh/setup-uv@v6` + `uv sync --frozen --extra dev
--no-install-project` 取代硬编码 pip 列表; 依赖单一来源 = pyproject.toml + uv.lock。

### P1-6 Testnet Operational Runbook ✅(commit 7caf626)

**交付**: `docs/testnet-runbook.md`(soak 用 .env / 启动健康自检 / 7 态监控清单 / 财务真相闭环验证 /
告警处置 / 证据收集 / 安全停机); `docs/runbook.md` 加交叉引用。

### P1-7 / P1-8 Soak 运行器 + 运行时证据记录 ✅(commit 402ca8a)

**交付**: `at01_common/soak.py`(子进程启动 run.py + 周期采样 /api/metrics + 证据逐行落盘
`logs/soak-evidence.jsonl` + 迁移检测 + 摘要); `tests/unit/test_v165_soak.py`(11 条纯逻辑)。

**诚实边界**: soak 主循环依赖真实 run.py + 测试网; **本会话测试网不可达**(smoke test 报
`Cannot connect to host testnet.binance.vision:443`), 故 7~24h 真实 soak 未执行, 仅交付可一键运行的
turnkey runner + 纯逻辑测试。**这使 L3 仍未触发** —— 见 `docs/testnet-operation.md`。

### P2 run.py 轻量抽取 ✅(commit a009438)

**交付**: `at01_common/bootstrap.py`(`inject_sys_path`)/ `at01_common/wiring.py`(`wire_system`,
承接原 initialize 全部装配)/ `at01_common/runtime.py`(`run`, 承接原 main 生命周期编排)。
`run.py` 1250→995 行; `AdaptiveTradingSystem` 仅 `initialize()` 委托 wire_system, 交易语义方法
(_on_signal/_risk_loop/_reconcile_loop/...)原样保留, 不微服务化。coverage 对 wiring/runtime 与
run.py 同理由 omit(编排粘合)。`tests/unit/test_v166_run_slim.py`(6 条)锁「结构不回归」。

---

## 验证

1. `.venv\Scripts\python -m pytest tests/ -q --cov --cov-report=term-missing --cov-fail-under=75 -m "not testnet"`
   → **1139 passed, 6 deselected**(96.91s); 覆盖率 **79.14%** ≥ 75%。
2. `ruff check .` → E9+F 全绿。
3. mypy 未新增(核心模块类型未变)。

## 诚实结论

V11.6 把「测试网验证 + 财务真相闭环」的**代码、测试、运维手册、一键 soak runner** 全部交付并钉死,
但**未虚报 L3**: L3(测试网无人值守实盘)的门槛是真实跑 7~24h 无人值守 soak, 而本会话测试网不可达
(环境因素, 非代码缺陷)。当前就绪等级仍为 **L2**; 恢复网络后按 `docs/testnet-runbook.md` §4 一键
`python -m at01_common.soak --hours 7` 即可启动验证, 详见 `docs/testnet-operation.md`。
