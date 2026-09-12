# 项目进度日志

> 记录**当前阶段**的关键交付与验证结论。更早的版本历史见
> [progress-archive.md](progress-archive.md)（V12.1 及更早）。
>
> 状态词严格区分「代码写了」与「真跑通了」：IMPLEMENTED / READY_TO_RUN / EXECUTED /
> PASSED / FAILED / BLOCKED / NOT_EXECUTED。**没跑过的就写没跑过。**

---

## 配置数据库化 / 守卫降摩擦 / 死开关清理 / Pi 漂移根因修复（进行中，2026-09-12）

任务单：`cc_task_db_config_usability.md`。缘起：操作者提出「模式切换与运行参数写数据库、
切换实盘别那么多验证、代码检查发掘优化项」。

### 范围裁定：拒绝「移除运行时闸门」

操作者要求「切换实盘无需卫士/闸门」。**区分了两类东西**：

| | A 启动前守卫 | B 运行时闸门 `TradingGate` |
|---|---|---|
| 时机 | 启动一次 | **每一笔单** |
| 处置 | **P2 降摩擦**(显式解锁) | **保持原样** |

依据是 Pi 实测：`orders_total=0` 而 `lifecycle=SAFE_MODE` —— 移除 B 不会让系统能交易，
只会让它拿一个被证明错 100% 的账本去下真钱单。

### P0 实盘权益基线从未与交易所对齐（已完成，**这是把测试网永久锁死的根因**）

`at50_risk/risk_manager.py` 的 `equity()` = `risk_initial_equity + realized + unrealized`，
而 `risk_initial_equity` **出厂默认 100000.0**，唯一覆盖它的逻辑(`mainnet_takeover`)
带 `not binance_testnet` 条件 —— **只在主网跑**。

于是 `live_testnet` 下：本地权益恒为 100000（与真实账户无关）→ 漂移 ≈ 100%
→ `equity_drift` 属 `_KILLED_ALONE`（单发即 KILL）→ SAFE_MODE → 无成交 →
漂移永不收敛 → **永久锁死**。

**Pi 现场数据印证**：`reconcile_drift_pct=1.0`、`reconcile_killed=81`、`breaker_kill=81`、
`recovery_streak=81`、`orders_total=0`；而市场健康、交易所健康、8 个任务全在跑、
`risk.state=NORMAL` —— **唯一异常就是这一条，是假阳性，不是真实资金风险**。

`settings.py` 里那句注释「V12 主网接管时由真实账户权益覆盖」—— `MainnetTakeover.takeover()`
只把快照写进 `hodl.record_baseline()` 用于 HODL 对比图，**从未回写过风控模型**，承诺一直没兑现。

修复：新增 `at01_common/live_equity.py`，任何非纸面模式启动时从交易所账户读取真实权益
覆盖基线。接线在守卫之后、**任何风控/策略组件构造之前**（它们构造期就读取该值）。
只读、无下单；播种失败或权益为 0 → **拒绝启动**（fail-closed）。
顺带修 `current_equity` 的 falsy 回落（`0.0` 被当 falsy → 账户真被清空时会静默回落成配置基线）。

**边界**：未放宽 `equity_drift` 的 KILLED 判定 —— 有测试锚定真实漂移仍照常 KILL。

### P2 启动守卫显式解锁（已完成）

`GUARD_OVERRIDE=<ISO8601 到期时间>:<确认短语>`。只解锁 A 类；**运行时闸门不受影响**
（有结构性测试：闸门源码里不得出现 `guard_override`）。带到期时间（无永久形态）、
fail-closed（短语错/过期/格式错一律照常拦截）、留痕（横幅 + `operator-status.guard_override`）。
默认行为逐字不变。

> ⚠️ 过程中 ruff F821 抓到一个真 bug：wiring 里 `import BANNER as _GUARD_BANNER` 却调用
> `BANNER`。**该分支零运行时测试覆盖**（需完整系统），若无静态检查会在真正解锁时 NameError。

### P3 死开关清理（已完成）

`MAINNET_READINESS_ENABLED` 从 `/admin` 移除 —— 它声明了但全代码库从不被读取
（主网自检在 wiring 里只要 `BINANCE_TESTNET=false` 就无条件执行）。上一轮靠帮文
标注「本开关无效」，但那仍是摆一个点了没反应的开关。同批移除 4 个同类死配置。

新增防回归守卫 `test_v126_dead_config_switches.py`。**写它时立刻抓出我自己的两处错误**：
把 `SYMBOLS` 错列进豁免（实际 `settings.symbol_list` 在读）、漏了 settings.py 导致把
`web_admin_auth` 误判成死开关（它在 `admin_auth_disabled` 属性里被消费）。

**顺带发现（未擅自处理）**：`BUY_DIP_PCT` / `SELL_PROFIT_PCT` 在 `/admin` 上可调，但只出现在
`strategy_version.TRACKED_PARAMS` 的字符串列表里，**没有任何策略读它们的值参与决策** ——
改它不产生行为变化，**优化器对这两个参数的建议是空转**。因被版本/管线按名字引用，
删除可能破坏参数追踪，已登记进 `KNOWN_UNCONSUMED` 显式豁免，待操作者决定接线还是撤下。

### P1 运行参数写入数据库（已完成）

新增 `runtime_config` / `runtime_config_history` 两张表（28 张表 / 277 列，
`SCHEMA_VERSION` V12.0 → **V12.1**）+ `at01_common/runtime_config.py`。
**优先级 DB > env > default**，启动时在 `init_db()` 之后、守卫与风控构造之前叠加。

三条硬边界（各有测试）：
1. **密钥永不入库** —— 复用 `config_store.is_sensitive_key`。DB 会被备份/拷贝，密钥入库等于扩散。
2. **bootstrap 关键项排除** —— `DATABASE_URL` 等存进自己指向的库是循环依赖。
3. **不热生效** —— 多数参数是构造期读取的，页面按 `restart_required` 如实提示。

**应用后强制重跑 `settings.validate()`** —— 否则一个非法的 DB 值会绕过启动期 fail-fast。

**一个意外但正确的结果**：allowlist 之外的三个字段（`WEB_ADMIN_TOKEN` / `SYMBOLS` /
`DAILY_REPORT_DIR`）**全部是 `editable=False`** —— 也就是管理页面上所有可编辑字段现在
都走数据库。于是 Pi 上不再需要挂载可写配置目录（此前需 `chown -R 999:999 /etc/adaptive-trading`），
这正是 P1 的目标。

**因此回滚语义必须做实**：配置分两层后只恢复 env 文件会让 DB 覆盖活下来 ——
操作者点了「恢复上一份配置」却发现值没回去。改为 `rollback_overrides()` 按审计
**逐步回退**（不是粗暴清空，那会丢掉与本次无关的覆盖），并合并报告两层成败。

**过程中被测试抓到的两个自己的错**：
- 百分比换算写反了域（`_to_env` 假定 UI 域，我直接喂了 Settings 域，0.03 变 0.0003）——
  与 V12.3 管理页面踩过的**同一个坑**。已把契约钉死为「`save_overrides` 收 UI 域、库存 env 域」并加测试。
- `apply_overrides` 的 `skipped` 分支不可达（`load_overrides` 已先过滤）—— 改成不过滤，
  让「白名单收缩后的孤儿行」可观测。

**变更了 3 条既有断言的语义**（按任务单要求在此说明理由）：`test_apply_writes_and_backs_up` /
`test_apply_reports_unwritable` / `test_rollback_endpoint` 断言的正是「写 env 文件」这套行为，
P1 的目标就是把它换成 DB 存储，故按新语义重写（分别改为「入 DB + 有审计」、
「文件不可写也能保存」、「按审计回退 DB 覆盖」）。

### 验收（P0/P1/P2/P3，全部完成）

```
ruff  All checks passed          mypy  Success (39 source files)
pytest -q -m "not testnet"  →  1499 passed, 6 deselected
      (1448 基线 + 11 P0 + 8 P3 + 18 P2 + 14 P1)
```

---

## V12.9 补充 — 三级验证流水线（Level 1/2 实测）

新增 `docs/verification/`：`local-verification.md` / `docker-verification.md` /
`pi-deployment.md`（Level 1 / Level 2 / Level 3）。

### 修复: 模式切换后重启服务被卡死（本机实测复现）

`.env` 遗留 `PAPER_TRADING=true` + DB `TRADING_MODE=testnet` → 冲突检查把**已被取代的**
env 值当操作者意图 → `RuntimeError` 拒绝启动。修法: `TRADING_MODE` 被 DB 覆盖时,
其**推导字段**一并从冲突判定剔除。**未放宽 §18**（同层矛盾照常 fail-closed, 有测试锚定）。
回归: `tests/integration/test_v129_mode_switch_reload.py`（7 条, 全链路非单函数）。

### 修复: Dockerfile COPY 行尾注释导致构建失败（V12.6 埋下, 实测才暴露）

`COPY ... # L9 展示` 的 `#` 被当成额外源路径 → `"/L9": not found`。
**从未真正跑过 `docker build`**（CI 的 docker-smoke 只在 GH Actions 跑）故一直没发现。
**这正是 Level 2 验证存在的意义。**

### Level 2 实测结果

| 项 | 结果 |
|----|------|
| DOCKER_BUILD | **PASSED**（镜像站 ARG, 301MB） |
| DOCKER_SMOKE（health / trading-mode） | **PASSED** |
| DOCKER_RESTART + DB 持久化 | **PASSED**（runtime_config + history 保留） |
| apply testnet（无密钥） | **BLOCKED —— 正确行为**（守卫 fail-closed） |
| DOCKER_COMPOSE_FULL | **NOT_EXECUTED** |
| PI_DEPLOY / PI_ARM64_SMOKE | **NOT_EXECUTED**（无 SSH） |

### 结论

**NOT_READY_FOR_PI** —— `DOCKER_COMPOSE_FULL` 与 `EVIDENCE_CHAIN` 两项未过。

---

## V12.9 — 小资金验证前最终工程收口（2026-09-12）

### P0-1 当前基线（**全部实测, 不引用历史数字**）

| 项 | 实测值 |
|----|--------|
| HEAD | `925ad9c`（本轮改动后另有新提交，见文末） |
| 分支 / 工作区 | `main` / 干净（仅运行时日报未跟踪） |
| `SCHEMA_VERSION` | **V12.1** |
| ORM 表数 / 列数 | **28 张 / 277 列** |
| ruff | All checks passed |
| mypy | Success (39 source files) |
| pytest `-m "not testnet"` | **1550 passed**, 6 deselected |
| 部署版本 vs 仓库版本 | **不一致** —— Pi 跑旧镜像（见 P0-5） |

### P0-3 TradingGate 结构性风险（已确认 + 已加固）

**全仓搜索 `ExecutionEngine.execute()` 调用点**：生产代码只有 **`run.py:343`（`_on_signal`）
与 `run.py:958`（`_apply_core_action`）两处**，其余全部是测试与 SQL 的 `session.execute`。
**无新增绕过路径** ⇒ 按任务单「保持当前实现，不做大改」。

按 §P0-3.4 补了**架构守卫测试** `tests/unit/test_v129_order_outlet_guard.py`（4 条），
把"审过一次"变成"**无法静默回归**"：
- 生产调用点一旦超出已审查清单即变红（强制后来者先回答"它过闸门了吗"）
- `run.py` 里闸门判定次数必须 ≥ `execute()` 调用次数
- 审查报告必须写明这条架构债；`execute()` docstring 必须提醒调用方负闸门责任

> 加的 docstring 触发了既有的 `test_executor_has_no_risk_permission_authority`
> （它断言该文件正文不得出现 `can_open_position`）—— 守卫按**全文搜索**判定，
> 注释里提一句也命中。**改的是我加的文字，不是测试**（任务单禁止为通过而改测试）。

### P1-4 `HOST_CONFIG_DIR` 卷（核实后保留，但角色已变）

按「必须搜索代码真实读取」核实：`resolve_config_path()` / `read_env_file()` **仍被调用**
（`build_config_view` / `build_draft` / `_preflight_restart`）。

**但角色变了**：V12.6 P1 起可编辑字段存数据库，该卷**不再是配置写入目标**，现在是
① 文件↔运行值差异对比 ② 文件回滚来源 ③ 密钥与 bootstrap 键的载体。
⇒ **保留**，并修正 `docker-compose.yml` 里那条已过期的注释（原写"使管理页面能读写配置文件"）。
**Pi 上配 DNS 不再需要 `chown -R 999:999`** —— 改模式/改参数走数据库即可。

### ❌ NOT_EXECUTED（如实标注，禁止伪造）

| 项 | 状态 | 原因 |
|----|------|------|
| P0-5 Pi 最新镜像闭环（build / up / ps / git_sha 比对） | **NOT_EXECUTED** | **无 SSH/部署执行能力**；只能 HTTP 只读 |
| P0-6 Pi `equity_drift` 假阳性闭环验证 | **NOT_EXECUTED** | 依赖 P0-5 |
| P1-5 三模式最终回归（逐组合实测） | 部分 | 有单测覆盖（含迁移冲突回归），**真实切换未做** |
| P1-1 operator-status 补字段 / P1-2 `/ops` 最终整理 | **未做** | 本轮聚焦 P0 |
| P1-3 交易前证据链逐字段审计 | **未做** | |
| P2 测试网真实测试 | **NOT_EXECUTED** | 需环境 |

### 用户需执行的最短命令（P0-5）

```bash
ssh <pi>
cd <repo> && git pull && git rev-parse HEAD      # 记录 HEAD
export IMAGE_TAG=$(git rev-parse --short HEAD)
export GIT_SHA=$(git rev-parse HEAD)
docker compose --env-file /etc/adaptive-trading/production.env build
docker compose --env-file /etc/adaptive-trading/production.env up -d
docker compose ps
curl -s localhost:8800/api/operator-status | head -c 400    # 应出现 trading_mode 字段
```
**判据**：`trading_mode` 字段出现 ⇒ 新镜像已生效；`reconcile_drift_pct` 回落 ⇒ P0-6 修复生效。
注意急停是**持久化**的，修复生效后仍需人工 `recover`。

### 验收

```
ruff All checks passed        mypy Success (39 files)
pytest -m "not testnet"  →  1550 passed, 6 deselected
```
**无真实 Pi 冒烟** —— NOT_EXECUTED。

---

## V12.8 — 真实环境验证 + 工程收口（2026-09-12）

任务单：操作者下发的 V12.8（真实环境验证 + 工程收口）。原则：**真实问题 > 真实运行验证 >
可观测性 > 数据一致性 > 部署稳定性 > 文档 > 代码美化**；禁止为"架构完整"大改。

### ✅ 已完成

**§3/§7/§16 真实交易链路审查** → 新增 [`docs/audits/trading-path-audit.md`](audits/trading-path-audit.md)

走读实际代码（非复述文档），每条给 file:line 依据：
- **订单出口唯一**：`ExecutionEngine.execute()`，全仓库只有 `run.py:343`(信号) 与
  `run.py:958`(核心仓) 两处调用，**均在闸门之后**
- **权限边界**：Web 10 个写端点无一涉及下单；AI 只写 `ai_advices`；策略只产 `Signal`
- **幂等三层已存在**（未重新设计）：`order_intents` DB 唯一键(重启不失效) +
  交易状态机闸门 + `newClientOrderId` 服务端去重
- **危险窗口**（请求已发/响应超时）先 `get_order` 反查、查不到才用**同一** client_order_id
  重发 ⇒ 五种场景下不会 BUY×3
- **12 类失败逐一确认"不可能下单"**，且原因可解释（`operator-status` + `risk_events`）

**§8 未消费配置最终裁决** —— 裁决：**撤下 UI 暴露（方案 B）**
- `BUY_DIP_PCT`：VWAP 折价行为**存在**，但由 `strategy_buy.py:55-56` 里**硬编码的 0.02**
  实现，从未读过该配置
- `SELL_PROFIT_PCT`：已被更通用的 `sell_take_profit_ladder` 取代
- **不选方案 A 的理由**：该配置默认 `0.005` 与硬编码 `0.02` **相差 4 倍**，接上去会
  **实质改变入场评分**；当前回测与运行都基于 0.02，小资金验证前做静默行为变更不可接受。
  这本身就是"它从未被接线"的证据。
- 已从 `FIELD_SPECS` / `Settings` / `TRACKED_PARAMS` / `PARAM_GROUPS` 移除，
  并列入 `test_field_stays_removed` 防复活

### ⚠️ 发现一个结构性脆弱点（未实施改动）

**闸门在 `execute()` 之外** —— 它在 `run.py::_on_signal` 里调用，因此"绕过 run.py
直接调 `execute()`"就会跳过闸门。**当前没有任何这样的调用点**，但这是
**「调用方记得调」而非「出口无法绕过」**。属架构改动，本轮按 §1「禁止为架构完整大改」
**未实施**，仅记录。

### ❌ NOT_EXECUTED（如实标注，禁止伪造）

| 任务单项 | 状态 | 原因 |
|---------|------|------|
| §13 Pi ARM64 真实部署验证（build / compose up / reboot / 断电恢复） | **NOT_EXECUTED** | **无 SSH 通道**，只能 HTTP 访问 Pi:8800 |
| §14 断电 / 重启恢复实测 | **NOT_EXECUTED** | 同上 |
| §15 网络异常实测（断网/超时注入） | **NOT_EXECUTED** | 仅单测覆盖，真实断网未做 |
| §11 `/ops` 最终重排 | **未做** | 本轮聚焦审查与裁决，未及 |
| §12 `operator-status` Q1–Q7 补字段 | **未做** | 同上（现有字段已能答 Q1/Q3/Q4/Q5，Q6/Q7 需补） |
| §18 交易前快照 / §19 证据链 | **未评估** | 现有 `decision_log` / `execution_events` / `evidence_chain` 可能已覆盖，未逐项核对 |

### Pi 实测证据（只读，能做的部分）

通过 HTTP 取到 Pi 实时状态：

```
mode = live_testnet | status = KILLED | can_buy = False | can_sell = False
uptime = 46688s   reconciled = False
last_error = 对账 / equity:equity_drift SOLUSDT
```

**两点结论**：
1. 响应里**没有 `trading_mode` 字段** ⇒ Pi 跑的是**旧镜像**（V12.7 的三模式未部署）
2. 仍卡在 V12.6 那个已修的 bug 上（`equity_drift` KILLED），已持续约 13 小时
   ⇒ **修复存在于仓库但未部署**。

### 验收

```
ruff All checks passed        mypy Success (39 files)
pytest -m "not testnet"  →  1546 passed, 6 deselected
```

**无真实 Pi 冒烟** —— 标记 NOT_EXECUTED。

---

## 运行模式体系简化（V12.7，2026-09-12）

操作者提出「选中模式 → 确认参数 → 模式启动」, 并给出完整任务单。核心思想:
**让架构承担复杂性, 不让操作者承担复杂性。**

### P0 TradingMode + ModeResolver

对外只有 `paper / testnet / live` 三个模式; `TRADING_MODE` 一旦设置即为权威,
解析结果**驱动** `paper_trading` / `binance_testnet` / `run_testnet_trading`。
因此既有守卫读到的仍是自洽的值 —— **TradingGate / RiskManager / ExecutionEngine
的逻辑一行未改**(§12/§13/§24)。

- **旧配置兼容(§17)**: 未设 `TRADING_MODE` 时按旧开关推导; 组合无法确定时 **FAIL CLOSED**
- **冲突 fail-closed(§18)**: `TRADING_MODE=live` + 显式 `PAPER_TRADING=true` → 拒绝启动并列出冲突项
- **行情源与模式正交(§7)**: 「模拟+主网行情」保留为**高级选项**, 不作为第四种模式

**一处刻意偏离任务单**: §4 写 `LIVE → live_trading_confirm=true`。若解析器代填,
§11 的"实盘特殊确认"即成走过场(那正是主网守卫要的输入)。故解析器只推导
"**我是什么模式**", "**我确认**"仍是显式输入 —— `TRADING_MODE=live` 缺确认即 fail-closed,
由页面确认弹窗去写这两个标志。

### P3 切换 API + 修 `testnet_gate` 越界

`GET /api/trading-mode` / `POST …/preview`(不写盘) / `POST …/apply`(只保存不重启, §20),
复用既有 admin 写鉴权(§19)。

**修了一处必须修的越界**: `testnet_preflight` 是**测试网**闸门, 却无条件执行、对主网真实
配置也返回 BLOCKED —— 而主网自己的两道守卫此刻已放行。两个模块意图相反, 严格的那个
静默获胜, `docs/mainnet-runbook.md` 那套流程**永远走不通**。改为只对测试网真实执行生效;
主网由主网守卫把关(门槛严格更高)。**门槛一项没减**。

> 这是本次唯一一处**放宽**, 依据是任务单 §11 明确描述实盘确认流程(⇒ LIVE 必须可达)。
> 回退方法写在提交信息里。

### P2 UI

`/admin` 第一层 = 三张模式卡 + 状态行(模式/行情源/标的/来源), 点选即生成 diff 与
**守卫预检**(不可启动的模式在卡片上就说明原因, 不再"存了才发现起不来")。
底层 Boolean 收进「高级配置」折叠区，**字段一个没删**。
`operator-status` 新增 `trading_mode` / `trading_mode_label` / `market_data_source`,
原有 `mode` 字段原样保留(向后兼容)。

实测(Playwright): 三卡渲染正确; 点实盘 → 真实资金参数表 + 「我已核对以上参数，确认进入实盘」。

### 迁移陷阱（实测中发现并修掉）

给本机 `.env` 显式加上 `TRADING_MODE=paper` 后实测发现一个隐蔽冲突：

`.env` 里往往还留着迁移前的显式 `PAPER_TRADING=true`。此时在页面上切到「测试网」，
`apply` 若**只写** `TRADING_MODE=testnet` 到数据库，启动时冲突检查会看到
「TRADING_MODE=testnet 但 PAPER_TRADING=true」而**拒绝启动** —— 而那是迁移期的旧值，
不是操作者的新意图。§18 的冲突检查本意是防"配置文件写得不一致"，不该被旧值卡住。

**修法**：`apply` 把 `MODE_DERIVED[target]` 里**在字段表内的**旧字段一并落库，
两边因此一致，冲突检查自然通过。补了两条回归测试（写入内容 + 写入后重启不冲突）。

> 注：`RUN_TESTNET_TRADING` 不在 `FIELD_SPECS` 内，因而不在入库白名单 ——
> 它由解析器在启动时按模式推导，不需要也不应手工落库。

### 遗留与诚实边界

- **P1(业务码减少 Boolean 组合)** 未做大规模改写 —— 按 §24「不要为架构优雅大规模重写」,
  改为让解析器**驱动**内部字段: 既有布尔读取因此仍然正确, 无需逐处替换。文档已说明。
- `/ops` 的 §14 重排未做(本轮聚焦 /admin 与 API)。当前 `/ops` 仍可用。
- 本次未在 Pi 上验证。

验证: 1542 passed / 6 deselected; ruff + mypy 全绿。

---

## 四种模式独立可切（批次 A，2026-09-12）

操作者给出四种模式的定义，并要求「在开关与参数表单中四种模式可方便切换、**无需相互依赖**」。

### 一处对不上的地方（先澄清再动手）

操作者写的「纸面模式：**模拟交易所数据**」与现状不符 —— 现行纸面模式用的是
**测试网真实行情**，只有**成交**在本地模拟。经确认选**甲**：保持现状，
四种模式摆成干净的 2×2（数据源 × 是否真实下单），而非引入新的合成行情源。
（顺带说明：要"用历史/合成数据验证策略"，系统里已有 `at80_backtest` 回测。）

### 真正挡路的是一道**挂错条件**的守卫

`主网观察`（`PAPER_TRADING=true` + `BINANCE_TESTNET=false`）此前无法启动。核实后确认
**这个模式没有任何真钱能力**：下单走 `PaperBroker`；三个用 REST 的对账器
（`PositionReconciler` / `OrderRecoveryEngine` / `ExchangeTruthReconciler`）全是
`rest_client=None if is_paper else ...`；`validate()` 也不要求主网凭证。

而两道守卫都挂在「**是否连主网**」上，不是「**是否可能用真钱下单**」上：
1. `mainnet_blocked_reason()`：只要 `BINANCE_TESTNET=false` 就要求 `LIVE_TRADING_CONFIRM=true`
2. `mainnet_readiness_check()` 的触发：`if not binance_testnet` —— 但这份九项清单本身就是
   "你即将拿真钱下单"的自检（第②项明确要求 `PAPER_TRADING=false`），挂错了位置

**关键论证：放宽这里没有打开任何意外真钱交易的路径。** 从主网观察改成主网真实需要
`PAPER_TRADING=false`，那会再次进入 `mainnet_blocked_reason()` 且**仍会被拦**。
拦 D 是一道挂错位置的重复守卫。

### 改动

- `mainnet_blocked_reason()`：纸面直接放行（并补 `observing_mainnet` 属性）
- 就绪自检触发条件：`not binance_testnet` → `not binance_testnet and not paper_trading`
  （`wiring.py` 与 `config_store.build_draft` 两处，保持同源）
- `MODE_NOTES` / 新增 `MODE_BLOCKED`：把「有说明」与「被守卫拦」分开 —— 主网观察现在
  有说明但可启动
- 启动时打醒目提示：**你在用主网真实行情**

### 顺带修掉 P1 的一个遗留缺口

改这条测试时发现 `_preflight_restart()` **只读 env 文件**里的值，而可编辑字段自 P1 起
存数据库 —— 也就是说「存了一份起不来的配置 → 重启后连页面都没了」这条防线**漏了 DB 那一层**。
已改为把 DB 覆盖一并纳入（用 UI 域喂 `build_draft`，与页面提交同域），并补测试。

### 测试

变更 4 条既有断言的语义（按任务单要求说明理由）：它们断言「主网观察被拦」这一旧行为，
现按新语义反转；另把 `test_mainnet_without_confirm_blocked` 等改为**显式** `paper_trading=False`
—— 原先靠 `_cfg` 的默认 `paper_trading=True` 走的其实是主网观察路径，测并非所测。
**真钱门槛的覆盖一条没少**，并新增一条「放宽 D 不得连带放宽 C」。

验证：`ruff` + `mypy` 全绿；`pytest -m "not testnet"` → **1502 passed**, 6 deselected。

---

## 管理页面分组框线 + 写接口鉴权默认关闭（2026-09-12）

### 分组框线（已完成）

管理页面的开关与参数原本只是「加粗标题 + 间距」，7 个模块挨在一起没有边界。
改为每个模块一个带框线区块：左侧 5px 色条按**语义权重**分档（运行模式蓝 / 风控参数红 /
策略参数紫 / 安全类橙 / 运行类青 / 运维类灰 / 只读项浅灰），组头给「模块名 + 项数 + 一句话说明」，
组内改了值则该组出现「已改 N 项未保存」+ 外发光。

浏览器实测：7 组色条各异；改一个风控参数脏标记正确落在「风控参数」组；
重置清零；**390px 窄屏横向溢出 0px**。

### 写接口鉴权默认关闭（操作者明确要求）

操作者要求「默认关闭鉴权、局域网访问都可以操作、局域网无其他人、方便优先」。

**改了两处，其中一处比要求更宽，如实说明**：
1. `.env` 加 `WEB_ADMIN_AUTH=off`（本机；`API_HOST` 保持 `127.0.0.1` 未暴露）
2. `settings.py` 的**出厂默认** `web_admin_auth` 由 `"on"` 改为 `"off"`

第 2 条的影响范围**超出操作者的两台部署** —— 任何新部署（含将来可能的主网机器）
都会默认关闭写接口鉴权。已在 `settings.py` 注释里如实列出代价（局域网内任何设备
无需凭据即可改配置 / 恢复急停 / 停机），并保留 `WEB_ADMIN_AUTH=on` + 非空 token 的
恢复路径。缓解措施是既有的三重可见性：启动横幅 + 页面常驻提示 + `/ops` WARN。

**同步变更 8 条既有断言的语义**（按任务单要求说明理由）：这些测试断言的是「鉴权打开时」
的行为，原先**隐式依赖默认值为 on**。现改为在 fixture / 用例里**显式设 `WEB_ADMIN_AUTH=on`** ——
这比依赖默认值更准确；另新增一条「显式 off 时非回环不再拦」覆盖新默认。
`test_default_is_auth_on` 反转为 `test_default_is_auth_off_per_operator_request`。

验证：`ruff` + `mypy` 全绿；`pytest -m "not testnet"` → **1500 passed**, 6 deselected。

---

## 工程结构与文档整理（已完成，2026-09-12）

任务：把「多版本迭代后已经看不懂」的仓库重新组织 —— 包名按架构层级整理、细化架构图、
补模式切换说明、整理工程文档。

### P0 基线（PASS）

`git status` 干净（仅一份本地 demo 产生的日报未跟踪）；`ruff` All checks passed；
`mypy` Success (39 files)；`pytest -m "not testnet"` → **1448 passed**, 6 deselected。

### P1 包名按阅读顺序重编号

**旧编号与数据流矛盾**：`at50_strategy` 与 `at50_execution` **撞号**，
且 `execution(50)` 排在 `risk(60)` **之后**却是更小的号 —— 靠编号推不出执行顺序。

新约定：**十位 = 层号（阅读顺序 = 数据流顺序），个位 0 = 主 / 5 = 同层辅助**。

| 新 | 旧 | 层 |
|---|---|---|
| `at01_common` | *(不变)* | L0 基础(横切) |
| `at10_market` | `at20_market` | L1 行情接入 |
| `at20_analytics` | `at30_analytics` | L2 分析 |
| `at30_strategy` | `at50_strategy` | L3 策略 |
| `at40_portfolio` | `at55_portfolio` | L4 组合 |
| `at50_risk` | `at60_risk` | L5 风控 |
| `at60_execution` | `at50_execution` | L6 执行 |
| `at70_journal` | `at40_journal` | L7 记录 |
| `at80_backtest` | `at70_backtest` | L8 研究-回测 |
| `at85_optimizer` | `at80_optimizer` | L8 研究-优化 |
| `at90_web` | `at10_web` | L9 展示(横切) |

`git mv` 保历史；169 个文件 / 765 处引用一次性正则改写（单次 alternation，非链式）；
同步 `bootstrap.py` / `conftest.py` 的 sys.path 列表、`Dockerfile` COPY、`pyproject.toml`
的 mypy files 与 coverage source —— 一并重排为层号顺序。**纯机械改名，无语义变更**：
改名后 `1448 passed` 与基线逐项一致。

### P2 清理死包与过期部署目录

三处部署目录重叠，其中一份**危险**：

| 目录 | 内容 | 处置 |
|------|------|------|
| `at90_deploy/` | 只剩 `init.sql`（Dockerfile/compose 早已删） | 死亡包，删除 |
| `90_deploy/` | **141 行手写 MySQL DDL** | **过期且危险**，删除 |
| `deploy/pi/` | `production.env.example` | 保留 |

`90_deploy/init.sql` 与 V11.0 的决策直接冲突（该版本明确「不手写表 DDL，表结构统一由
ORM 负责，避免与 models.py 漂移」），正是被废弃的那版，注释里点名的问题（`orders` 缺
`reduce_only`/`accounting_state`）依然存在 —— **照着它建库会建出缺列的表结构**。

正确版本（仅 `CREATE DATABASE`）归入 `deploy/init.sql`；新增 `deploy/README.md` 说明
**Dockerfile/compose 为何必须留在根目录**（compose 卷用相对路径，挪走会让运行中的
生产容器挂到空目录）。

### P3 零散文件归位

18 份 `cc_task_*.md`（2811 行）散在根目录 → `docs/tasks/`；`01_design/` → `design/`。
归档件**保留当时的旧包名不改写**（改写历史会让记录失真），改为在 `docs/tasks/README.md`
给映射表并标明。根目录现在只剩代码包 + 构建文件 + 文档目录。

### P4 架构图重做

`architecture.md` 原为「竖着一长条 ASCII，模块与流程混在一起」，重写为：
分层总图 + 成交时序图（标出四个卡点）+ **四套状态机关系图**（此前散在三份文档里没一处讲清）
+ 数据流分叉 + 周期任务表 + 26 张表 + 设计决策。

- 分层图**刻意用 ASCII**：分层是结构关系不是流程，而 Mermaid 的 `flowchart` 在跨子图
  连线时会忽略 `direction LR` —— 实测把领域链堆成 **1161×1840** 的一列，比原图更难读。
- 时序图/状态机图/闭环图用 Mermaid，**逐张渲染成 PNG 目视验证**后才定稿。
- 写准一处文档与实现不符：`TradingGate` 自称「六维」，V11.6 的 BUY 安全契约又收了两维
  （停机窗口 / 关键后台任务健康），**实为 6+2**。
- 新增 `scripts/check_docs_mermaid.py`：对文档里的 mermaid 块做真实 parse 校验。
  Mermaid 语法错**不报错**、只渲染成空白；更隐蔽的是「语法合法但布局崩掉」—— 本脚本
  正是被这次踩坑逼出来的。

### P5 模式切换说明

新增置顶 **§0.5 一页纸速查**（原手册 396 行，紧急时翻不动）：

- **四种开关组合真值表**，含那个**存在但不可用**的第四种：`主网纸面观察` 被两道守卫
  必然拦截（`mainnet_blocked_reason()` 只要 `BINANCE_TESTNET=false` 就要求
  `LIVE_TRADING_CONFIRM=true`，**即便纸面**；`mainnet_readiness_check()` 第②项要求
  `PAPER_TRADING=false`）。`/api/operator-status` 里确有 `paper_mainnet` 这个**上报值**，
  但**它起不来**。
- 切换决策图（Mermaid，已渲染目视验证）+ 五条可行路径速查 + 三种改配置方式
- **怎么确认切成功**：唯一权威是 `/api/operator-status` 的 `mode`；并明确「`mode` 只说明
  配置是什么，能不能下单看同一个响应里的 `can_buy`」

§9 从 4 行要点展开为**逐条步骤**（A→B / B→A / B→C / C→B/A + 通用收尾），每条给
「改什么 → 怎么应用 → 重启 → 确认生效 → 回滚」。补入原文档没有的实操细节：换 key 后须
`docker compose up -d` 而非 `restart`（env_file 变更需重建容器）；纸面模式**不会**撤回
已提交的真实挂单。

**修正一处错标**：主网就绪自检在 4 份文档里写作「八维」，但 `mainnet_readiness.py` 的
代码注释明确编号 `# 1.` ~ `# 9.` —— **实为九项**。同一份手册内部也自相矛盾
（§1 列了 ⑨ 项却标「八维」，§9 写「九项全绿」）。已统一。

### P6/P7 文档索引与瘦身

- 新增 `CLAUDE.md`（仓库导览：冻结红线 / 三道闸门 / 安全契约 / 阅读路线 / 常用命令 /
  本机启动的坑 / 提交约定 / 诚实原则）
- 新增 `docs/README.md`（按问题索引，不按文件名罗列；每份文档标注
  🟢当前态 / 🟡状态快照 / ⚪历史存档；含「冲突时以谁为准」权威来源表）
- `progress.md` **1251 → 429 行**，历史移入 `progress-archive.md`；修掉两处
  「已完成 / 待执行」自相矛盾的标签
- **过期数字按实测修正**：`Base.metadata` → **26 张表 / 265 列**；
  `SCHEMA_VERSION` → **V12.0**；测试数 **1236 → 1448**；README 版本 V12.1 → V12.4。
  带版本归属的历史陈述保持原样（如「V10.7 新增 … 共 25 张」描述的是当时的事实）
- README 去掉内嵌的过期架构图（**停留在 V9.0**，与代码脱节后没人发现）→ 改指
  `docs/architecture.md`，消除「同一事实两份副本」的分叉源

### P8 验收（PASS）

```
ruff  All checks passed
mypy  Success: no issues found in 39 source files
pytest -q --cov --cov-fail-under=75 -m "not testnet"
      → 1448 passed, 6 deselected, coverage 80.46% ≥ 75%  (exit 0)
相对链接自检  124 条, 0 断链
Mermaid 校验  4/4 图语法通过
构建面一致性  Dockerfile COPY 的 11 个包名与实际目录逐一相符; py/构建面无旧包名残留
```

**运行时复核**：本机以 `DATABASE_URL=sqlite:...` 覆盖启动（本机无 MySQL/Redis），
`/` `/admin` `/ops` 均 200，`/api/operator-status` → `paper_testnet` / `TRADING` /
`can_buy=true`。

### 诚实边界

- 本次为**结构性重构 + 文档整理**，未改动任何交易语义；`TradingGate`、主网守卫、
  测试网守卫、`settings.validate()` 的判定一律未放宽。
- 未验证项：Docker 镜像未重新构建（`docker-smoke` 只在 CI 跑）；Pi 上的生产容器未更新
  —— 包名变更后**旧镜像仍可运行**，但下次重构镜像前需确认 Pi 侧部署命令不受影响。
- 文档里的「八维」错标存在了很久没被发现，说明**文档与代码的一致性没有自动化保障**
  （Mermaid 校验是第一个，但只覆盖图语法）。

## 管理页面 / 配置控制台（已完成，2026-09-12）

任务单：`cc_task_admin_config_console.md`（P0–P9）。分工：`/` 看状态、`/admin` 改配置、
`/ops` 上线前只读自检。

### P0 上线前检查（PASS）

- `git status --short`：仅 `docs/progress.md` 已修改 + 任务单未跟踪；无 .env / 密钥 / 数据库 / 日志。
- `ruff` → `All checks passed!`；`mypy` → `Success: no issues found in 39 source files`；
  `pytest -m "not testnet"` → **1340 passed**，coverage **76.67%** ≥ 75%（exit 0）。

### P3/P5/P6 配置存储层 `at01_common/config_store.py`

- 字段定义表 `FIELD_SPECS`（25 项，7 组）：模式 / 运行 / 安全 / 运维 / 风控 / 策略 / 只读。
  每项含 label、help、单位、推荐值、范围、是否需重启、是否敏感、是否可编辑。
- **百分比参数以百分数呈现**（UI 填 `3` = 3%，内部存 `0.03`），不让操作者猜 `0.05` 的含义。
- 敏感字段（含 `KEY`/`SECRET`/`TOKEN`/`PASSWORD`）不可编辑、不回显，只报「已配置 / 未配置」。
- `build_draft()` 校验**复用启动期同源判定**：`settings.validate()`、
  `mainnet_blocked_reason()`、`mainnet_readiness_check()`。
- 写入：写前备份 `<file>.bak.<UTC 微秒时间戳>Z`（保留 20 份）、只改目标键、
  保留注释与顺序、临时文件 + `os.replace` 原子替换、保留权限位；`rollback()` 可恢复。

### P1/P2/P3/P4/P5/P7/P8 `/admin` 页面

- 新增 `at90_web/static/admin.html` + `at90_web/web_admin_routes.py`。
- 首屏状态条（模式 / 状态 / 买入 / 卖出）；配置解释**默认折叠**，标题给一句摘要。
- 运行模式四张卡片；**开关与参数合一表单**，按类别分组，每项带说明与推荐值。
- 草稿三段式：改 → 预览（只校验、只展示，**不写文件**）→ 保存（写盘 + 提示重启）。
- 管理操作区沿用二次确认弹窗，主网真实模式下确认文案点明真实资金。
- 令牌存 `localStorage`，仅本机浏览器。

### 四个由测试/实测发现的真实缺陷（均改实现，未改断言）

1. **百分比单位换算缺失**：`build_draft` 把 UI 的 `3`（3%）直接塞进 `Settings`，而 Settings 存小数。
   不修会让 `validate()` 把 3.0 判为越界 —— 或更糟：静默写入比预期大 100 倍的阈值。
2. **`live_trading_confirm` 类型错**：该字段在 Settings 里是 **str**（守卫用 `.strip().lower()`），
   塞布尔值会让 `mainnet_blocked_reason()` 抛 `AttributeError`。
3. **回滚会毁掉自己的备份**：备份时间戳只有秒级精度，「保存后立刻回滚」在同一秒内生成同名备份，
   后者覆盖前者 —— 等于把要恢复的那份毁掉。改为微秒精度 + 同名兜底 + 恢复源先读入内存。
4. **百分比假「待重启」**：`_settings_to_ui` 绕道 `_to_env`（假定 UI 域）再除一次 100，
   把 0.05 变 0.0005，导致所有百分比参数被误报为「与文件不一致」。

### 守卫行为记录：`主网纸面观察` 不可用

任务单 P3 列出该模式（`PAPER_TRADING=true` + `BINANCE_TESTNET=false`），但**现有守卫必然拦它**：

- `mainnet_blocked_reason()`：只要 `BINANCE_TESTNET=false` 就要求 `LIVE_TRADING_CONFIRM=true`，**即便纸面**；
- `mainnet_readiness_check()` 第②项要求 `PAPER_TRADING=false`，纸面必然不满足。

**未放宽任何守卫**：管理页面如实把该模式标注为「当前守卫下无法启动」并给出原因，
选它做 draft 会被守卫拒绝（有单测锚定）。

### 另一处诚实标注：`MAINNET_READINESS_ENABLED` 是空开关

该字段在 `settings.py` 声明，但**全代码库从未被读取** —— 主网就绪自检在 `wiring.py` 中只要
`BINANCE_TESTNET=false` 就**无条件执行**。因此把它置 `false` **不会**关闭自检。
页面**不给**该开关假风险警告（那是假警报），只如实说明现状（有单测锚定文案）。

### P9 测试与文档

- 新增 `tests/unit/test_v12_admin_config_console.py`（**72 条**）：敏感判定与掩码、
  配置视图、四模式草稿、非法百分比/阈值/止盈阶梯拒绝、主网真实不可绕过、
  写入保留注释与权限、备份唯一性、回滚、以及上述四个缺陷的回归锚定。
- 更新 `README.md`（三入口说明 + API 表）、`docs/operating-modes-manual.md`（§5.y 管理页面）、
  `docs/runbook.md`（配置保存 / 重启 / 回滚 / 让容器可写）。

### 浏览器实测（纸面模式，EXECUTED）

在本机以临时配置（**无任何真实密钥**）启动 `run.py`，用 Playwright 走完整流程：

- `/admin` 首屏：状态条、折叠的配置解释（摘要「当前：纸面 + 测试网，真实资金不会被使用」）、
  四张模式卡（主网纸面观察标红并说明守卫原因）、分组开关与参数（百分比按 % 显示）。
- 切「测试网真实」→ 预览：diff `PAPER_TRADING true → false` + 风险提示「关闭纸面 = 真实下单」。
- 切「主网真实」→ 预览：**被守卫拒绝**（缺 `MAINNET_API_SCOPE_CONFIRM`），
  且**文件未被改动、备份数为 0** —— 守卫在写入前就拦住了。
- 改 `RISK_MAX_SINGLE_ORDER_PCT` 5 → 3 → 预览显示 `5.0% → 3.0%` → 保存：
  文件写入 `0.03`（单位换算正确）、备份保留旧值 `0.05`、注释完整、
  提示「需重启服务/容器生效」+ 重启命令（正确识别为非容器环境）。
- 回滚：恢复 `0.05`，且回滚前的当前配置另存了一份（可再次回退）。
- 移动端 390px：`scrollWidth == clientWidth == 390`，无横向溢出。
- 三个内联脚本均通过 `node --check`。

### Pi 生产端落地（EXECUTED，2026-09-12）

按操作者选定方案「同时改 compose 挂载」在真机执行：

- `/opt/adaptiveTrading` 拉到 `295c899`；`production.env` 追加 `HOST_CONFIG_DIR=/etc/adaptive-trading`。
- 权限调整：`chown -R 999:999 /etc/adaptive-trading`（宿主上即 `lxd:docker` = 容器内 `app`），
  目录 `700`、文件 `600`，**root 仍可读写**；文件仍非 world-readable。
- 重建容器后挂载出现 `/etc/adaptive-trading -> /etc/adaptive-trading`。

**过程中发现并修正一处自己的疏漏**：首次重建时 `IMAGE_TAG`/`GIT_SHA` 仍钉在 `25acbb1`，
导致镜像标签与实际运行的代码（`295c899`）不一致 —— 正是 `/ops` 要查的追溯性问题。
已同步为 `295c899` 并重建，容器内 `printenv GIT_SHA` 与 `IMAGE_TAG` 均正确。

**Pi 端实测（全部 PASS）**：

- `/admin` 200、`/ops` 200、容器 `healthy`，镜像 `adaptive-trading:295c899`。
- `/api/admin/config` → `path=/etc/adaptive-trading/production.env`、**`writable=true`**、
  25 字段、令牌仅显示 `<configured>` 且 `editable=false`（响应中无真实令牌）。
- **保存实测**：`LOG_LEVEL` INFO → WARNING 经 `apply` 写入成功，生成备份
  `production.env.bak.20260911T192455898363Z`，权限保持 `600`，重启提示正确给出容器命令
  `docker compose --env-file /etc/adaptive-trading/production.env up -d`。
- **回滚实测**：恢复 `LOG_LEVEL=INFO`，diff 确认除该项外与备份**完全一致**。
- **守卫实测**：经 API 尝试切主网真实（`PAPER_TRADING=false` + `BINANCE_TESTNET=false`
  + `LIVE_TRADING_CONFIRM=true`）→ `stage=validate` 拒绝，理由含缺 `MAINNET_API_SCOPE_CONFIRM`
  与缺主网 key；**文件未被改动、未新增备份**。
- 数据仍在 `/srv/adaptive-trading/data` 且持续写入；`operator-status` 仍为
  `测试网真实下单 / KILLED / can_buy=false`（既有 SAFE_MODE 未变）。

### 追加：页面内重启 + 令牌状态提示（2026-09-12）

操作者反馈「预览失败(401)」且希望「管理页面支持直接修改重启」。做了两件事：

**1. 页面内重启**（`POST /api/admin/restart`，需令牌）

- 复用与 Ctrl+C / `docker stop` **完全相同**的优雅停机路径：新增
  `at01_common/runtime.py::request_shutdown()` 置位 `run()` 已在等待的 stop_event，
  **没有新增任何停机逻辑**。
- **重启前先做启动守卫自检**：当前配置文件过不了 `validate()` / `mainnet_blocked_reason()` /
  `mainnet_readiness_check()` → **拒绝重启**（防「存了坏配置一重启服务就起不来」）。
- **如实回报能否被拉起**：容器内 → `restart: unless-stopped` 会拉起；非容器 → 明确告知
  不会自动回来、需手动启动。页面自动轮询等服务恢复。

**2. 发现并修复一个既有 bug**：`/api/shutdown` 只置
`system_state.extra["shutdown_requested"] = True`，而**全代码库没有任何地方读它** ——
该端点自诞生起就是**空操作**，却固定返回 `{"ok": true}`。现改为投递真实停机请求并如实回报结果。
顺带明确：容器内 `restart: unless-stopped` 会把退出的容器重新拉起，因此容器里「请求停机」
**实际等同重启**；要真正停下需 `docker compose stop`（页面已写明）。

**3. 401 体验**：新增 `GET /api/admin/auth-check` 探针，令牌输入框旁常驻状态徽章
（未填写 / 令牌有效 / 令牌无效 / 服务端未启用）；401 与 503 都改为给出可操作提示
（含「令牌可在服务器上 `grep WEB_ADMIN_TOKEN <配置文件>` 查看」），不再只甩原始 detail。

**一个既有测试按真实意图更新**：`test_v150_web_security.py::test_authorized_shutdown`
原先断言 `{"ok": True}` —— 那正是在断言空操作的行为。改为断言该测试真正关心的内容：
鉴权放行、标志置位、**不谎报投递成功**。

**Pi 实测（EXECUTED）**：

- 部署 `169261e`，镜像 `adaptive-trading:169261e`。
- 本地（非容器）：点重启 → 进程优雅停机（`正在停止…` → 监督器取消 9 个后台任务 →
  WebSocket/行情引擎停止 → `系统已停止`，exit 0），页面如实提示不会自动拉起。
- **Pi（容器）**：`POST /api/admin/restart` → `ok=true / containerized=true` →
  容器**自动重启**（`StartedAt` 变化、`RestartCount=1`），约 20 秒回到 `healthy`，
  接口 200、`git_sha=169261e2…`；急停态按设计跨重启保持（仍 `KILLED`）。
  **验证了 `restart: unless-stopped` 在进程 exit 0 后确实会拉起容器这一关键假设。**
- `ruff` / `mypy` 全绿；全量 **1421 passed**，coverage **80.36%**。

### 未执行 / 待办

- Pi 上既有 **SAFE_MODE（对账漂移 100%）根因仍未排查** —— 与本任务无关，保持原状。
- 本次**未触发任何主网动作**，主网仍未上线。

## 个人管理页面配置任务单（任务单下发记录，2026-09-12 — 该工单已完成，见上方同名段落）

- 新增 `cc_task_admin_config_console.md`：给 CC 的可执行任务清单，覆盖 `/admin` 个人管理页面、可折叠当前配置解释、模式切换、开关说明、常用参数配置、配置保存/回滚、管理操作和 `/ops` 上线检查入口。
- 任务口径：个人本地内网使用，不展开多用户权限和密钥管理；但仍保留现有交易安全闸门、主网守卫、测试网守卫和配置校验，不新增绕过后端的交易入口。

## Dashboard 易用性优化（已完成，2026-09-12）

任务单：`cc_task_dashboard_usability.md`（P0–P7）。边界：只改 Web/API 展示层与只读聚合，
**未改动**交易策略、`TradingGate`、主网守卫、测试网守卫或 `settings.validate()` 的任何判定。

### P0 上线前检查（PASS）

- `git status --short`：仅 `docs/progress.md` 已修改 + 任务单未跟踪；**无 .env / 密钥 / 数据库 / 日志 / 证据文件**。
- `uv run ruff check .` → `All checks passed!`
- `uv run mypy` → `Success: no issues found in 39 source files`
- `uv run pytest -q --cov --cov-report=term-missing --cov-fail-under=75 -m "not testnet"`
  → **1301 passed, 6 deselected**，coverage **79.15%** ≥ 75%（exit 0）。

### P1 新增只读聚合接口

- 新增 `at90_web/web_operator_status.py`（纯函数，无 I/O，便于独立测试）：
  `resolve_mode` / `explain_switches` / `suggest_next_action` / `build_operator_status`。
- 新增 `GET /api/operator-status`（`web_api_routes.py`）。**交易许可单一权威**：
  `can_buy` / `can_sell` / `status` 全部透传 `build_runtime_health`（其本身取自 `TradingGate`），
  展示层不重新判定；闸门未就绪时 fail-closed 为 `false`。
- 返回 `mode` / `mode_label` / `risk_level` / `status` / `can_buy` / `can_sell` / `summary` /
  `next_action` / `write_actions_enabled` / `dangerous_actions` / `switches` / `deploy` / `runtime`。
- 系统未完全启动时返回稳定 JSON，不抛 500（快照失败降级为「闸门未就绪」）。
- `WEB_ADMIN_TOKEN` 只暴露「已配置 / 未配置」，**有单测断言其值不出现在响应里**。

### P2/P3/P5 Dashboard 首屏

- `at90_web/static/index.html`：新增 **运行结论卡**（模式徽章 + 状态徽章 + 买入/卖出许可 +
  阻断原因 + 下一步建议），按 `risk_level` 着色（绿/橙/红/深红）。
- 新增 **当前配置解释卡**：`PAPER_TRADING` / `BINANCE_TESTNET` / `LIVE_TRADING_CONFIRM` /
  `MAINNET_API_SCOPE_CONFIRM` / `WEB_ADMIN_TOKEN` 五开关的人话含义。
- 前端集中维护 `STATUS_DICT`（7 个状态的中文标签 + 解释），与 Python `STATUS_META`、
  `docs/operating-modes-manual.md` §5 三处对齐；英文状态保留为 badge 小字便于调试。
- 数据源 `/api/operator-status` 独立 5 秒轮询 —— WS 无数据时首屏仍不空白，
  显示「系统启动中 / 交易闸门未就绪」。

### P4 危险写操作

- 「恢复急停」「解除熔断」「停机」一律弹二次确认框，框内列出**当前模式 / 当前状态 / 要执行的动作 /
  操作后果**；主网真实资金模式下额外加上「⚠️ 当前是【主网真实资金】模式」前缀。
- **急停不设确认**（冻结是安全方向，应即时可用），但常驻导航栏醒目位置。
- 新增右上角令牌输入框，仅存浏览器 `sessionStorage`，不上传、不进 URL；写操作带 `X-Admin-Token`。
- 请求失败显示服务端 `detail` / `msg`，**不静默失败**；成功后自动刷新 `/api/operator-status`
  与 `/api/metrics`。

### P6 只读部署检查页

- 新增 `at90_web/static/ops.html` + `GET /ops`。只读，**不含任何写接口调用**（有单测断言）。
- 9 项检查给出 `PASS / WARN / BLOCKED` 三态并汇总最差项：API 可达、`/api/metrics.health` 可读、
  运行状态、交易许可、运行模式、代码版本可追溯、写令牌、最近对账、后台任务、最近错误、
  数据/日志目录（如实显示「未暴露」）。

### P7 测试与文档

- 新增 `tests/unit/test_v12_dashboard_usability.py`（**34 条**）：四模式判定、七状态映射、
  许可 fail-closed 透传、令牌不泄露、危险动作清单、deploy/runtime 透传。
- 扩充 `tests/integration/test_web_api.py::TestOperatorStatus`（**5 条**）：接口形状、
  引擎未就绪不 500、令牌不泄露、`/ops` 只读、Dashboard 结论卡标记存在。
- 更新 `README.md`（API 表 + Web Dashboard 使用说明）、`docs/operating-modes-manual.md`（§5.x）。
- **修了一个真实缺陷**：`can_buy=false` 时摘要曾丢掉卖出侧信息，导致 `REDUCE_ONLY` 不显示
  「允许卖出减仓」——由单测发现，已修实现而非改测试。
- `.gitignore` 补 `*.db-shm` / `*.db-wal`（`*.db` 覆盖不到的 WAL 副文件）与 `.playwright-cli/`。

### 本地实机展示（纸面模式，EXECUTED）

- 启动：`DATABASE_URL=sqlite+aiosqlite:///./adaptive.db REDIS_ENABLED=false uv run python run.py`
  （本地无 MySQL/Redis，按 runbook 的 SQLite 零依赖默认跑）。生命周期到达 `TRADING`，
  测试网行情 WS 已连。
- `/api/operator-status` 实测：`paper_testnet` / `TRADING` / `risk_level=safe` /
  `can_buy=true&can_sell=true` / `write_actions_enabled=true`；**响应中不含令牌值**。
- 浏览器实测（Playwright）：结论卡绿底、配置解释卡正常；点「急停」后状态转 `KILLED` 且
  「重启不会自动恢复」提示到位；「恢复急停」确认框正确列出模式/状态/后果；
  令牌缺失与令牌错误**均显示服务端 `detail`（未授权）**；`/ops` 报 `WARN`（理由准确：
  本地裸跑未注入 `GIT_SHA`）；移动端 390px 下 `scrollWidth == clientWidth`，无横向溢出。
- 两个内联脚本通过 `node --check` 语法校验。

### 诚实边界

- **未做主网真实模式的可视化验证**：`live_mainnet` 需要主网凭证，本任务不碰主网，
  故只通过单测锚定其 `tone=danger` 与 `BINANCE_TESTNET=false` 的红色开关提示。
- 本地展示实例仍在运行（`127.0.0.1:8800`，纸面模式，不产生真实订单），仅为本机演示。

## Dashboard 易用性优化任务单（任务单下发记录，2026-09-12 — 该工单已完成，见上方同名段落）

- 新增 `cc_task_dashboard_usability.md`：给 CC 的可执行优化方案，聚焦首屏运行结论、模式横幅、开关解释、危险操作二次确认、状态翻译、Pi 上线部署检查页。
- 任务边界：只改 Web/API 展示与只读状态聚合，不改变交易策略、交易闸门、主网守卫、测试网守卫或风控判定；交易许可仍以 `runtime_health.can_buy/can_sell` 为准。

## 运行模式手册（新增文档，2026-09-11）

- 新增 `docs/operating-modes-manual.md`：把「纸面 / 测试网真实执行 / 主网实盘」三种模式的
  **配置判据、启动守卫链、运行时状态含义、运维动作速查、模式切换与回退清单**收口到单一入口。
- 判据与阈值均从代码核实（非经验总结）：`settings.py::validate()/mainnet_blocked_reason()`、
  `testnet_gate.py::testnet_preflight()`、`mainnet_readiness.py::mainnet_readiness_check()`
  （九项）、`wiring.py::wire_system()` 的实际守卫顺序、`trading_gate.py` 六维三接口、
  `runtime_health.py` 的 `status` 分类优先级与 `can_buy/can_sell` 契约、`risk_killswitch.py` 的
  「不自动复位」语义、`web_api_routes.py` 的四个写端点与 `X-Admin-Token`。
- 澄清的两处易错点：①「急停」与「熔断器」不同 —— 熔断器有 cooldown 自动复位，急停必须人工
  `POST /api/emergency/recover` 且重启不复位；②`WEB_ADMIN_TOKEN` 为空时写接口返回 **503**（fail-closed）
  而非 401。
- 交叉链接已补：`README.md` 文档索引、`runbook.md` §各运行模式。
- 本文档**不改变任何代码或运行态**，纯文档交付；主网仍未上线，无任何主网动作。

## Pi root 快速部署（已完成，2026-09-11）

任务单：`cc_task_pi_root_quick_deploy.md`（root + 本地内网 + 快速上线，不做公网暴露）。

### §1 上线前代码检查（本地开发机，PASS）

- `git status --short`：仅 `docs/progress.md` 已修改 + `cc_task_pi_root_quick_deploy.md` 未跟踪；
  **无 .env / 密钥 / 数据库 / 日志 / 证据文件**。
- 部署 Git SHA：`25acbb1`（`feat: prepare external Pi production deployment`）。
- `uv run ruff check .` → `All checks passed!`
- `uv run mypy` → `Success: no issues found in 39 source files`
- `uv run pytest -q --cov --cov-report=term-missing --cov-fail-under=75 -m "not testnet"`
  → **1301 passed, 6 deselected**，coverage **79.90%** ≥ 75%（exit 0）。
- Compose 渲染：`ADAPTIVE_TRADING_ENV_FILE=deploy/pi/production.env.example docker compose --env-file deploy/pi/production.env.example config` → 成功；
  确认 `ports: 8800:8800`、四个 bind mount 指向 `/srv/adaptive-trading/{data,logs,evidence,reports}`、`image: adaptive-trading:<IMAGE_TAG>`。

### §2 目标机现状复核（发现：非全新主机）

- 目标：Raspberry Pi 4/5，Ubuntu 22.04.5 LTS，**aarch64**，LAN `192.168.50.156`，root 可 SSH（密钥登录）。
- Docker/Compose **已安装**（server/client `29.8.0`，Compose `v5.5.1`），故任务 §3 无需重装。
- `/opt/adaptiveTrading` **已存在一份 2026-09-08 的部署**：`main` @ `5d61898`，工作区干净；
  容器 `adaptive-trading:5d61898` 已 `Up 2 days (healthy)`，但仅发布到 `127.0.0.1:8800`（**未开内网**），
  数据落 `/opt/adaptiveTrading/{data,logs,evidence}`，配置来自仓库内 `.env`（非外置 `/etc`）。
- `/etc/adaptive-trading` 与 `/srv/adaptive-trading` **尚不存在**。
- 磁盘：`/` 为 `/dev/sda2`（110G，已用 39%，可用 66G）。`/srv` 与根同盘，**无独立 SSD 挂载**。
- 网络：Pi 可访问 GitHub（`git ls-remote` 得到 `main` = `25acbb1`，与本地部署 SHA 一致）；
  `pypi.org` 200、`pypi.tuna.tsinghua.edu.cn` 200；**`registry-1.docker.io` 不可达**（SSL 握手断开）→
  基础镜像必须走镜像站（已有 `.env` 使用 `docker.1ms.run/library/python:3.13-slim`）。

### §2b 现网运行态（需操作者确认后再重建）

- 容器有效环境为 `PAPER_TRADING=false` + `BINANCE_TESTNET=true` + `RUN_TESTNET_TRADING=1`
  → 处于**测试网真实执行**配置（非纸面）。
- 但 `/api/metrics` 显示 `health.status = KILLED`、`lifecycle.state = SAFE_MODE`
  （`kill_switch.armed=true`，原因「对账矩阵 KILLED: equity:equity_drift SOLUSDT」，
  `reconcile_drift_pct = 1.0` 远超阈值 `0.02`；`orders_total = 0`）。
  → 急停已武装，实际**无法下单**；累计 `RestartCount = 10`。
- 现有 `.env` 含真实测试网 key（64 字符）与 48 字符 `WEB_ADMIN_TOKEN`，权限 `664`（偏宽），
  且 `AI_ENABLED=true`（deepseek）、`REDIS_ENABLED=true`。**未在聊天/日志/文档中展示任何密文值**。

### 操作者决策（重建前确认）

- 交易配置：**沿用现网配置**（不强制回到纸面）——保留 `PAPER_TRADING=false` + `BINANCE_TESTNET=true`
  + `RUN_TESTNET_TRADING=1` + 测试网 key + `AI_ENABLED=true` + `REDIS_ENABLED=true`。
- 数据：**复制到 `/srv` 并保留旧目录**（`/opt/adaptiveTrading/data` 原地留作回滚备份）。

### §3 Docker（已装，未重装）

- 目标机为 Ubuntu 22.04.5 LTS aarch64，`docker` 与 `docker compose` 已存在：
  server/client **29.8.0**，Compose **v5.5.1**；`systemctl is-enabled docker` → `enabled`。故跳过安装步骤。
- 内网可达性：`ufw` inactive、`iptables INPUT policy ACCEPT` → 端口发布到 `0.0.0.0` 后局域网可直接访问。
- 时钟：NTP active、`System clock synchronized: yes`（交易时间戳依赖）。

### §4 代码（Pi）

- `/opt/adaptiveTrading` 工作区干净，`git pull --ff-only` 快进 10 个提交：
  `5d61898` → **`25acbb1`**（full `25acbb1dadf2867beec75862812ce097bdf02019`），与本地部署 SHA 一致。
- 以 `sudo -u pi` 执行 git，仓库内文件属主保持 `pi:pi`。

### §5 外置 env

- `/etc/adaptive-trading/production.env`，权限 **600 root:root**；
  以现网 `.env` 为底逐键搬运（**脚本内比对键名，不回显任何值**）：替换 5 键
  （`API_HOST`→`0.0.0.0`、`API_PORT`→`8800`、`DATABASE_URL`→SQLite、`IMAGE_TAG`/`GIT_SHA`→`25acbb1`），
  追加 5 键（`ADAPTIVE_TRADING_ENV_FILE` 指回自身 + 4 个 `HOST_*_DIR`→`/srv/adaptive-trading/*`）。
- `docker compose --env-file /etc/adaptive-trading/production.env config` 渲染成功：
  `image: adaptive-trading:25acbb1`（非裸 `latest`）、`ports: 8800:8800`、4 个 bind 指向 `/srv`。
- 未搬运模板里的 `TZ=Asia/Shanghai`（现网容器实际 `TZ=UTC`，加它会改变行为）；
  未搬运主网凭证（现网 `BINANCE_API_KEY`/`SECRET` 本就为空，模板要求留空）。
- **遗留提醒**：原 `.env`（`/opt/adaptiveTrading/.env`，`664`）仍在原地，内含已失效的 MySQL DSN；
  未删除以作回滚备份，后续若手动执行 compose 而未带 `--env-file` 会读回旧配置。

### §6 构建与启动（arm64 实测，首次 EXECUTED）

- `registry-1.docker.io` 在本网络不可达（SSL 握手断开）→ 用镜像站参数
  `PYTHON_BASE=docker.1ms.run/library/python:3.13-slim`、`PIP_INDEX_URL`/`UV_DEFAULT_INDEX=mirrors.aliyun.com`。
- `docker compose build` **exit 0**，产出 `adaptive-trading:25acbb1`（310MB，aarch64）。
- `up -d` 重建容器：`image=adaptive-trading:25acbb1`、`restart=unless-stopped`、
  `0.0.0.0:8800->8800/tcp`、bind 全部指向 `/srv/adaptive-trading/{data,logs,evidence,reports}`。
- **`docs/raspberry-pi-deployment.md` §8 中的「arm64 真实构建/运行未执行」至此变为已执行。**

### §2b 数据迁移

- 停机前只读 `PRAGMA integrity_check` → **ok**（26 表，`trades` 32038 行）。
- `docker stop` 优雅停机 **Exited (0)**（SIGTERM → run.py 优雅停机，WAL 已 checkpoint）。
- `cp -a` 复制 `data/logs/evidence/reports` 到 `/srv/adaptive-trading/*`，`chown -R 999:999`（容器内 `app` 用户）；
  源目录（41M）保留。复制后完整性与表数复核：integrity `ok`、27 表、`kill_switch_state` 1 行。

### §7 内网访问验证（PASS）

- 本机 `http://127.0.0.1:8800/api/health` → **200** `{"status":"ok","running":true}`
- 内网 `http://192.168.50.156:8800/api/health` → **200**（同一响应）
- 内网 Dashboard `http://192.168.50.156:8800/` → **200**，18726 字节
- `/api/metrics` → 返回 `snapshot` + `health` 全量字段；
  未配置任何路由器端口转发，`0.0.0.0` 仅暴露在局域网（写接口由 `WEB_ADMIN_TOKEN` fail-closed 保护）。

### §8 重启自恢复验证（PASS）

- 下发 `reboot` → Pi 约 2 分钟后恢复上线（轮询 19 次）。
- Docker `active`/`enabled` 随系统启动；容器**自动恢复** `healthy`，`RestartCount=0`。
- 重启后 `/api/health` 本机与内网均 **200**，Dashboard 内网 **200**。
- `kill_switch_state` 1 行、`armed=True`、`lifecycle=SAFE_MODE` ——
  **急停冻结态跨重启保持，不自动复位**（符合设计契约 `docs/raspberry-pi-deployment.md` §6.3）。
- 数据连续：`trades` 32049（重启前）→ **32072**（重启后）；`adaptive.db-wal` 持续写入 `/srv/adaptive-trading/data`。
- 同机其他家庭服务（AdGuard/MySQL/青龙/1Panel-redis）亦全部自动恢复。

### §9 完成回填

```text
[2026-09-11 21:38] Pi root quick deploy
- Pi LAN IP: 192.168.50.156
- Git SHA: 25acbb1dadf2867beec75862812ce097bdf02019 (short 25acbb1)
- Docker version: 29.8.0 (server/client)
- Compose version: v5.5.1
- Env path: /etc/adaptive-trading/production.env
- Data path: /srv/adaptive-trading/data
- Container status: running, healthy, RestartCount=0
- Health: 200 {"status":"ok","running":true} (loopback + LAN)
- Metrics: 200 (snapshot + health 全字段)
- Reboot recovery: PASS
- Mainnet/live trading status: TESTNET
```

- 交易状态判定依据：`PAPER_TRADING=false` + `BINANCE_TESTNET=true` + `RUN_TESTNET_TRADING=1`
  → 配置上属**测试网**；但当前运行时 `health.status=KILLED`、`lifecycle=SAFE_MODE`、
  `kill_switch.armed=true`（原因「对账矩阵 KILLED: equity:equity_drift SOLUSDT」，`reconcile_drift_pct=1.0`
  远超阈值 `0.02`），**实际无法下单**，容器日志可见交易闸门逐条拦截 BUY/SELL 信令。
- **主网未上线**：`LIVE_TRADING_CONFIRM` 空、`MAINNET_API_SCOPE_CONFIRM=false`、主网凭证留空；
  本次部署**未触发任何主网动作**。

### 未完成 / 遗留（诚实披露）

- **对账漂移 100% 未排查**：本任务范围是「跑起来 + 内网可达 + 重启自恢复」，未处理该 SAFE_MODE 根因；
  该漂移源自更早的部署（`RestartCount` 已 10），本次数据迁移把它一并带入。
- **`/srv` 与根同盘**：目标机无独立 SSD，`/srv/adaptive-trading` 落在系统盘 `/dev/sda2`（110G，可用 66G），
  未做 SSD 迁移。
- **未做备份 timer / 7h·24h soak / 主网只读接管**：均在本任务范围之外，状态与 `cc_task_pi_small_capital.md` 一致。
- **`.env` 权限**：仓库内旧 `.env` 仍为 `664`（新外置 env 已 `600`）；建议后续收紧或删除。
- **口令卫生**：排查过程中 `DATABASE_URL`（Pi 本地 MySQL）口令被打印进会话记录，未入库未入文档；
  建议轮换该口令。

## Pi 生产部署与主网小资金交接任务（待执行，2026-09-11）

- 新增 `cc_task_pi_production_mainnet_handoff.md`，将“完成生产环境 Pi 部署并进入 Binance 主网运行”拆成 P0-P7 可验收任务：工程质量门槛、Pi 基础准备、外置生产配置、arm64 构建、纸面 24h、备份/恢复/重启演练、测试网真实执行与 7h/24h soak、主网只读接管、24h 不下单观察、人工批准后的首笔小资金交易。
- 追加 `cc_task_pi_root_quick_deploy.md`，用于“root 用户 + 本地内网 + 简单快速”的生产 Pi 部署：安装 Docker、拉取 main、创建 `/etc/adaptive-trading/production.env`、使用 `/srv/adaptive-trading` 持久化目录、Compose 构建启动、内网访问验证、重启自恢复验证。
- `cc_task_pi_root_quick_deploy.md` 已补充上线前代码检查：部署前先确认 `git status`、Git SHA、`ruff`、`mypy`、非 testnet 测试覆盖率和 Compose 配置渲染通过，再执行 Pi 部署。
- 当前工程进度复核：代码侧已有 Docker 外置 env、SSD bind mount、主网就绪自检、启动对账、急停、备份脚本和主网清单；但真实 Pi arm64 部署、生产外置配置、systemd 备份 timer、测试网 7h/24h soak、主网只读接管与主网交易仍未执行。
- 安全边界保持不变：CC 可以部署和收集上线证据，但不得自行批准首笔 Binance 主网真实交易；首笔小资金动作必须由操作者单独 go/no-go 批准并记录。

## Pi 生产准备与小资金前置任务（待执行，2026-09-09）

- Compose 支持以 `docker compose --env-file /etc/adaptive-trading/production.env` 读取仓库外配置；`ADAPTIVE_TRADING_ENV_FILE` 将同一文件注入容器，运行数据/日志/证据/日报可分别映射到 Pi SSD。
- 新增 `deploy/pi/production.env.example` 与 `cc_task_pi_small_capital.md`。任务单将 CI 类型安全、arm64 实测、外置备份/恢复、局域网防护、24h 纸面和测试网 7h/24h soak 列为小资金前置门槛。
- 状态诚实披露：Pi arm64、外置配置、备份 timer、测试网 soak 与主网动作均**尚未执行**；该改动仅提供部署契约与操作任务，不改变「主网未上线」结论。
