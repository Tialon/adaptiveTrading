# 项目进度日志

> 记录**当前阶段**的关键交付与验证结论。更早的版本历史见
> [progress-archive.md](progress-archive.md)（V12.1 及更早）。
>
> 状态词严格区分「代码写了」与「真跑通了」：IMPLEMENTED / READY_TO_RUN / EXECUTED /
> PASSED / FAILED / BLOCKED / NOT_EXECUTED。**没跑过的就写没跑过。**

---

# 项目进度日志

> 记录每个开发阶段的关键交付与验证结论

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
