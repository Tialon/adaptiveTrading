# 运行模式使用与运维手册

> 本文是「**按模式选配置 + 按模式运维**」的单一入口。所有判据均取自代码
> (`at01_common/settings.py` / `testnet_gate.py` / `mainnet_readiness.py` / `wiring.py` /
> `at50_risk/trading_gate.py`)，不是经验总结。
>
> 分工：通用启动/排障/API 速查见 [runbook.md](runbook.md)；测试网无人值守专册见
> [testnet-runbook.md](testnet-runbook.md)；主网专册见 [mainnet-runbook.md](mainnet-runbook.md)；
> 容器与 Pi 见 [docker-deployment.md](docker-deployment.md) / [raspberry-pi-deployment.md](raspberry-pi-deployment.md)。

---

## 0. 三种交易模式（一句话判据）

判据只有两个布尔量：`PAPER_TRADING` 与 `BINANCE_TESTNET`。

| 模式 | 判据 | 真实下单? | 连哪家交易所 | 用途 |
|------|------|-----------|--------------|------|
| **A 纸面** | `PAPER_TRADING=true` | ❌ 模拟成交 | 行情连交易所，下单不发出 | 出厂默认、策略迭代、长期验证 |
| **B 测试网真实执行** | `PAPER_TRADING=false` + `BINANCE_TESTNET=true` + `RUN_TESTNET_TRADING=1` | ✅ 真实下单 | `testnet.binance.vision` | 端到端执行闭环、soak |
| **C 主网实盘** | `PAPER_TRADING=false` + `BINANCE_TESTNET=false` + `LIVE_TRADING_CONFIRM=true` | ✅ 真实下单 | `api.binance.com` | 极小资金真实运行（需人工 go/no-go） |

> **模式 A 的边界**：只有「下单」被模拟。行情、分析、策略、风控、生命周期、生命周期闸门、
> 每日复盘与实盘**完全同一条代码路径**；持仓对账器不接 REST（退化为纸面现金自检）。
> 所以纸面能验证的是「逻辑与状态机」，不能验证的是「交易所真实行为（拒单/部分成交/滑点/API 限流）」。

**跨模式不变量（三种模式都成立）**
- 冻结产品边界：单交易所 Binance / 单币种 SOLUSDT / **现货（非合约）** / 双仓 / 低频。
  `SYMBOLS` 非 SOLUSDT 会被 `validate()` 直接拒绝启动。
- **唯一的下单咽喉是 `ExecutionEngine.execute()`**，且只经 `TradingGate` 放行；Web 无下单端点，
  AI 只写 `ai_advices`，不参与下单路径。
- 风控/急停/对账在任何模式下都不关闭。

---

## 1. 启动守卫链：决定「能不能起来」

按 `at01_common/wiring.py::wire_system()` 的**实际顺序**执行，任一关失败即
`RuntimeError` → **拒绝启动**（不是降级运行）：

```
1. settings.validate()                 配置审计
2. settings.mainnet_blocked_reason()   默认禁主网
3. mainnet_readiness_check()           主网八维自检   ← 仅当 BINANCE_TESTNET=false
4. testnet_preflight()                 测试网真实执行闸门
5. 载入 DB + kill_switch.load_from_db()  恢复持久化急停态
```

| 关 | BLOCKED 条件（任一命中） | 报告块 |
|----|--------------------------|--------|
| 1 配置审计 | 实盘却缺对应环境的 API key/secret；标的非 SOLUSDT；三桶比例和 ≠ 1.0；风控阈值不在 (0,1]；回撤档位非严格递增；**非回环 `API_HOST` 但 `WEB_ADMIN_TOKEN` 为空**；端口非法；无启用策略；DB 地址为空等 | 日志 `生产配置审计未通过` |
| 2 默认禁主网 | `BINANCE_TESTNET=false` 且 `LIVE_TRADING_CONFIRM != "true"` | 日志 `拒绝主网启动` |
| 3 主网八维 | ①非主网 ②`paper_trading=true` ③`LIVE_TRADING_CONFIRM!=true` ④`MAINNET_API_SCOPE_CONFIRM!=true` ⑤标的非 SOLUSDT ⑥配置审计有项 ⑦急停已冻结 ⑧`git_sha` 为空 ⑨端点不含 `api.binance.com` | `=== MAINNET READINESS ===` |
| 4 测试网闸门 | 纸面模式 → 直接放行（`mode=paper`）；非纸面时须**同时**满足：`BINANCE_TESTNET=true` + `live_trading=false` + `RUN_TESTNET_TRADING=1` + 测试网 key/secret 齐备 | `=== TESTNET PREFLIGHT ===` |

> 第 3 关的 `kill_switch_armed` 传 `False`（此刻尚未从 DB 载入），急停态由第 5 步之后的
> 生命周期守卫兜底：**已武装 → 停在 `READY` 不交易**。两处互不重复。

**看报告**：启动日志里搜 `=== MAINNET READINESS ===` / `=== TESTNET PREFLIGHT ===`，
`BLOCKED:` 行会逐条列出原因。**不要 bypass —— 逐条修配置后重启。**

---

## 2. 模式 A：纸面（Paper）

### 配置
```ini
PAPER_TRADING=true
BINANCE_TESTNET=true          # 行情走测试网流
PAPER_INITIAL_CASH=100000     # 纸面初始资金 USDT
PAPER_FEE_RATE=0.001          # 纸面手续费率
RUN_TESTNET_TRADING=           # 留空 —— 有值也不影响(纸面直接放行)
```
无需任何 API key（`validate()` 只在 `PAPER_TRADING=false` 时才要求凭证）。

### 启动与验收
```bash
python run.py                                   # 本地裸跑
docker compose up -d                            # 或容器
curl -fsS http://127.0.0.1:8800/api/health      # {"status":"ok","running":true}
curl -fsS http://127.0.0.1:8800/api/metrics     # 看 health.can_buy / state
```
预期：`=== TESTNET PREFLIGHT ===` 中 `paper_trading=true`，**无 BLOCKED 行**。

### 能用 / 不能用
- ✅ 策略与参数迭代、风控与生命周期演练、对账链路演练、每日复盘、面板全功能。
- ❌ 不能证明交易所侧真实行为：拒单、部分成交、`-2010` 类下单错误、真实滑点、API 权重限流。
- ⚠️ 纸面资金是**纯账面**：`AccountLedger` 仅纸面落库，实盘锚定交易所真相对账。

---

## 3. 模式 B：测试网真实执行（Real Testnet）

### 配置
```ini
PAPER_TRADING=false
BINANCE_TESTNET=true
RUN_TESTNET_TRADING=1                        # 显式 opt-in, 少了这行启动即 BLOCKED
BINANCE_TESTNET_API_KEY=<testnet key>
BINANCE_TESTNET_API_SECRET=<testnet secret>
LIVE_TRADING_CONFIRM=                        # 必须保持空 —— 置 true 会触发闸门拒绝
BINANCE_API_KEY=                             # 主网凭证一律留空
BINANCE_API_SECRET=
STARTUP_RECONCILE_ENABLED=true               # 实盘必开
```
> `live_trading=true` 会被第 4 关判定为 `主网实盘, 测试网闸门禁止` → BLOCKED。
> 这是刻意的：**测试网闸门与主网守卫互斥**，不可能「配错一个变量就连上主网下单」。

### 启动与验收
```bash
python run.py            # 或 docker compose --env-file /etc/adaptive-trading/production.env up -d
```
预期报告：
```
=== TESTNET PREFLIGHT ===
symbol=SOLUSDT  paper_trading=false  binance_testnet=true  live_trading=false
git_sha=<完整 SHA>  credentials_present=true
```
无 `BLOCKED:` 行 = 放行。

### 无人值守 soak
```bash
python -m at01_common.soak --hours 7 [--paper] [--port 8800] [--interval 60] [--no-launch]
```
- 默认 `PAPER_TRADING=false`（**真实下单**）；加 `--paper` 才是纸面 soak。
- `--no-launch` 只采样已运行实例，不另起 `run.py`。
- 证据落 `logs/soak/<run_id>/evidence.jsonl`；验收契约见 `at01_common/soak.py::evaluate_soak_result`。

### 能用 / 不能用
- ✅ 真实下单→成交→账本→对账全闭环；崩溃恢复、UNKNOWN 订单收敛、部分成交。
- ❌ 测试网流动性/撮合与主网不同，**不能**据此推断主网滑点与成交质量。
- ⚠️ 测试网余额是水龙头发的，资金曲线无参考价值。

---

## 4. 模式 C：主网实盘（Mainnet）

### 配置
```ini
PAPER_TRADING=false
BINANCE_TESTNET=false                        # 连主网
BINANCE_API_KEY=<主网 key, Spot only, 关提现/关资金转移>
BINANCE_API_SECRET=<主网 secret>
LIVE_TRADING_CONFIRM=true                    # 守卫 2: 必须显式 true
MAINNET_API_SCOPE_CONFIRM=true               # 守卫 3 第④项: 人工核对 key 权限后置 true
MAINNET_READINESS_ENABLED=true
MAINNET_TAKEOVER_ENABLED=true                # 首次只读接管(账户快照+对账+HODL 基线)
STARTUP_RECONCILE_ENABLED=true
EQUITY_RECONCILE_TOLERANCE_PCT=0.02
# 首次小资金: 由操作者填入经书面批准的保守上限, 不得留空
RISK_MAX_POSITION_PCT=
RISK_MAX_SINGLE_ORDER_PCT=
RISK_MAX_SOL_EXPOSURE=
RISK_MAX_DAILY_LOSS=
RISK_MAX_DRAWDOWN=
RISK_INITIAL_EQUITY=
```
> `BINANCE_API_KEY` 权限**无法由 API 自证**，所以第④项只能是人工核对后的显式开关，
> 默认 `false` → 主网默认必拦。

### 启动会依次发生
1. 守卫 1/2/3 全部放行 → 打印 `=== MAINNET READINESS ===`（9 行全绿）。
2. **首次只读接管**（仅当尚无 HODL 基线）：账户快照 + 对账 + 记录基线。
   接管**未通过 → 自动武装急停**，不带着未确认的仓位开始交易。
3. **启动对账**：崩溃窗口恢复；有未解决差异 → **自动武装急停**。
4. 生命周期推进：急停已武装则停在 `READY`（不交易），否则进 `TRADING`。

### 上线纪律（不可妥协）
- **CC / 自动化不得自行批准首笔主网交易** —— 必须由操作者单独 go/no-go 并留记录。
- 首次建议**观察 ≥ 24h 不下单**，确认行情/对账/健康快照稳定。
- 资金递增：只有当前级别连续稳定 + 对账恒平衡 + 无 `KILLED` 才可小幅递增，且重过
  [mainnet-readiness.md](mainnet-readiness.md) 复审。
- **禁止**：一次性放大资金 / 杠杆 / 合约 / 加币种 / 高频 / LLM 自动下单。

---

## 5. 运行时状态：一个词回答「现在能不能下单」

### 权威字段
`GET /api/metrics` → `health`：

| 字段 | 含义 |
|------|------|
| `status` | 单一运行状态：`KILLED` / `RECOVERY` / `PAUSED` / `REDUCE_ONLY` / `DEGRADED` / `TRADING` / `SAFE` |
| **`can_buy` / `buy_block_reason`** | **权威**：直接取自 `TradingGate.can_open_position()`，绝不虚报「可买」 |
| **`can_sell` / `sell_block_reason`** | 权威：取自 `TradingGate.can_reduce_position()` |
| `lifecycle.state` | 十态机：`INIT→WARMING_UP→SYNCING→SELF_CHECK→READY⇄TRADING`，`DEGRADED`/`RECOVERY`/`SAFE_MODE`/`STOPPED` |
| `risk.state` | 五态：`NORMAL`/`REDUCE_ONLY`/`PAUSED`/`KILLED`/`RECOVERY_CHECK` |
| `kill_switch.armed` + `.reason` | 持久化急停（**不自动复位**，重启后仍冻结） |
| `breaker.fund_action` | 资金熔断档位：`NONE`/`REDUCE_ONLY`/`PAUSE`/`KILL` |
| `reconcile.reconciled` / `market.connection_ok` / `market.data_healthy` / `exchange.healthy` | 四个健康维 |
| `tasks.running` / `tasks.failed` | 关键后台任务健康（崩溃 → 禁开仓） |

### `status` 分类优先级（`classify_runtime_status`）
```
KILLED      急停已武装 / lifecycle=SAFE_MODE|STOPPED / risk=KILLED
  ↓
RECOVERY    risk=RECOVERY_CHECK / lifecycle=RECOVERY
  ↓
PAUSED      risk=PAUSED / 熔断器打开 / fund_action∈{PAUSE,KILL}
  ↓
REDUCE_ONLY risk=REDUCE_ONLY / fund_action=REDUCE_ONLY
  ↓
DEGRADED    lifecycle=DEGRADED
  ↓
TRADING     lifecycle=TRADING          ← 唯一「正常可开仓」
（其余）    SAFE
```

### 六维闸门（`TradingGate`，开仓必须**六维全绿**）
`SystemLifecycle.can_trade` ∧ `RiskState.can_trade` ∧ 行情健康 ∧ 交易所健康 ∧ 对账健康 ∧ 资金熔断。
外加两项前置：**非停机窗口** ∧ **关键后台任务健康**。
三接口：`can_open_position()` / `can_reduce_position()` / `can_cancel_order()`（撤单是风险收敛动作，
除 `STOPPED` 外放行）。
**方向语义**：`SAFE_MODE` 禁买、数据可信时允许安全减仓；`KILLED` 买与卖**都禁**（急停冻结）。

### 5.x 在 Dashboard 上看这些

不用记字段名，打开面板看两处即可：

| 位置 | 回答什么 |
|------|----------|
| **首屏「运行结论卡」** | 当前模式（纸面 / 测试网真实 / 主网）、当前状态中文解释、买入与卖出许可、阻断原因、下一步建议 |
| **「当前配置解释」卡** | `PAPER_TRADING` / `BINANCE_TESTNET` / `LIVE_TRADING_CONFIRM` / `MAINNET_API_SCOPE_CONFIRM` 的人话含义，以及写操作是否启用 |
| **`/ops` 部署检查页** | 只读自检（PASS / WARN / BLOCKED）：版本可追溯、`/api/health`、`/api/metrics.health`、写令牌、最近对账、最近错误 |

结论卡的数据来自 `GET /api/operator-status`（只读聚合）。它的 `can_buy` / `can_sell` 与 `status`
**直接取自同一份 `runtime_health`**（其本身取自 `TradingGate`），展示层不重新判定交易许可 ——
所以面板显示「禁止买入」时，引擎侧一定也是禁止的，不存在两套口径。

颜色对应关系：绿/灰 = 纸面或安全；橙 = 测试网真实或降档状态；**红 = 主网真实资金或急停冻结**。

> 注意：Compose 的 `healthy` **不等于**「可买入」。容器健康只说明 HTTP 服务可达；
> 能不能交易只看结论卡的「买入许可」。

### 5.y 在 `/admin` 里理解和切换模式

`http://<host>:8800/admin` 是**改配置**的地方（`/` 只看状态，`/ops` 只做上线前自检）。

页面上四张模式卡片，点一下就把对应开关填进下方表单；**但要点「保存配置」才真正落盘**：

| 卡片 | 开关组合 | 能否启动 |
|------|----------|----------|
| 纸面模式 | `PAPER_TRADING=true` + `BINANCE_TESTNET=true` | ✅ 可启动（出厂默认） |
| 测试网真实 | `PAPER_TRADING=false` + `BINANCE_TESTNET=true` | ✅ 可启动（还需 `RUN_TESTNET_TRADING=1`） |
| 主网纸面观察 | `PAPER_TRADING=true` + `BINANCE_TESTNET=false` | ⛔ **当前守卫下无法启动**（见下） |
| 主网真实 | `PAPER_TRADING=false` + `BINANCE_TESTNET=false` + `LIVE_TRADING_CONFIRM=true` | ⚠️ 需两道确认齐全 |

> **「主网纸面观察」为什么不可用**：它看起来最安全（纸面 + 主网行情），但现有两道守卫都会拦它——
> ① `settings.mainnet_blocked_reason()`：只要 `BINANCE_TESTNET=false` 就要求
> `LIVE_TRADING_CONFIRM=true`，**即便 `PAPER_TRADING=true`**（刻意为之：防误配直连主网）；
> ② `mainnet_readiness_check()` 第②项要求 `PAPER_TRADING=false`，纸面必然不满足。
> 管理页面**不绕过**这两道守卫，只如实标注该模式不可用。要跑主网行情观察，请在纸面+测试网下进行。

**改配置的三段式**（`草稿 → 预览 → 保存`）：

1. **改**：开关/参数直接在页面上调；百分比参数按**百分数**输入（填 `3` 表示 3%，不用猜 `0.03`）。
2. **预览改动**：只校验、只展示，**不写文件**。会列出「当前 → 改为」的 diff、
   风险提示（关掉对账/连主网等标红）、以及校验不通过的具体项。
3. **保存配置**：写配置文件（**写前自动备份**），并明确提示**需要重启才生效**。

**安全边界**（页面无法越过的红线）：
- `LIVE_TRADING_CONFIRM` 与 `MAINNET_API_SCOPE_CONFIRM` **必须人工显式确认**，页面不能代为设置成"可用"；
  缺任一项，主网真实模式的草稿会被守卫直接拒绝，**文件一个字都不会被改**。
- 校验复用 `settings.validate()` / `mainnet_blocked_reason()` / `mainnet_readiness_check()`，
  与正常启动时的判定**同源**，不是另写一套。
- 密钥类字段（`*_KEY` / `*_SECRET` / `*_TOKEN`）**不可编辑、不回显**，只显示「已配置 / 未配置」。
- `SYMBOLS` 已冻结为 SOLUSDT，页面只读。

**保存后如何生效 / 如何回滚**：见 [runbook.md](runbook.md) §配置保存与回滚。

---

## 6. 运维动作速查

### 6.1 写接口（全部需 `X-Admin-Token` 头）
```bash
TOKEN=$(grep -E '^WEB_ADMIN_TOKEN=' /etc/adaptive-trading/production.env | cut -d= -f2-)
BASE=http://127.0.0.1:8800

curl -fsS -X POST -H "X-Admin-Token: $TOKEN" $BASE/api/emergency/kill      # 急停: 冻结 + 撤全部挂单
curl -fsS -X POST -H "X-Admin-Token: $TOKEN" $BASE/api/emergency/recover   # 解除急停(唯一解冻方式)
curl -fsS -X POST -H "X-Admin-Token: $TOKEN" $BASE/api/breaker/reset       # 重置熔断器(有 cooldown 自动复位)
curl -fsS -X POST -H "X-Admin-Token: $TOKEN" $BASE/api/shutdown            # 请求优雅停机
```
- `WEB_ADMIN_TOKEN` 为空 → 写接口返回 **503**（fail-closed，不是 401）。
- 令牌不匹配 → **401**。用 `secrets.compare_digest` 比对。
- **急停 ≠ 熔断器**：熔断器有 cooldown 会自动复位；急停**必须人工 `recover`**，且**重启不会复位**。

### 6.2 只读接口（无需令牌）
`/` 面板 · `/api/health` · `/api/system` · `/api/metrics` · `/api/risk` · `/api/positions` ·
`/api/orders` · `/api/signals` · `/api/equity-curve` · `/api/strategy-performance` ·
`/api/market` · `/api/analytics` · `/api/regime`

### 6.3 动作 × 模式适用性

| 动作 | 命令 | A 纸面 | B 测试网 | C 主网 |
|------|------|:------:|:--------:|:------:|
| 启动/停止/重启 | `docker compose up -d` / `stop` / `restart` | ✅ | ✅ | ✅ |
| 看能不能下单 | `curl /api/metrics` → `can_buy` | ✅ | ✅ | ✅ |
| 人工急停 | `POST /api/emergency/kill` | ✅ | ✅ | ✅ |
| 解除急停 | `POST /api/emergency/recover` | ✅ | ✅ | ⚠️ **先人工确认差异收敛** |
| 重置熔断器 | `POST /api/breaker/reset` | ✅ | ✅ | ⚠️ 同样先查根因 |
| 优雅停机 | `POST /api/shutdown` 或 `docker compose stop` | ✅ | ✅ | ✅ |
| 备份数据库 | `python scripts/db_backup.py` | ✅ | ✅ | ✅ **必需** |
| 版本追溯 | `docker inspect ... GIT_SHA/IMAGE_TAG` | ✅ | ✅ | ✅ **必需** |
| soak | `python -m at01_common.soak --hours 7` | `--paper` | 默认真实 | ❌ 不用 |

### 6.4 备份与版本追溯
```bash
# 备份前先 integrity_check, 再用 sqlite3 在线备份 API 生成带 UTC 时间戳副本到 data/backups/
python scripts/db_backup.py --db data/adaptive.db --backup-dir data/backups --keep 30
python scripts/db_backup.py --db data/adaptive.db --check-only      # 仅校验完整性
# 退出码: 0=成功 / 1=DB 缺失·完整性失败·备份失败
```
```bash
docker inspect adaptive-trading --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -E 'GIT_SHA|IMAGE_TAG'
```
生产镜像**必须**用 git SHA 打 tag，**禁止裸 `latest`**。

---

## 7. 部署模式（跑在哪）

| 部署 | 启动 | 配置来源 | 数据落点 |
|------|------|----------|----------|
| 本地裸跑 | `python run.py` | 仓库 `.env` | `./adaptive.db` `./logs` |
| Docker（本地/CI） | `docker compose up -d` | 仓库 `.env` | `./data` `./logs`（bind） |
| **Pi 生产** | `docker compose --env-file /etc/adaptive-trading/production.env up -d` | **`/etc/adaptive-trading/production.env`（600）** | **`/srv/adaptive-trading/{data,logs,evidence,reports}`** |

Pi 生产要点（2026-09-11 实机验证）：
- 配置与源码分离：`production.env` 内 `ADAPTIVE_TRADING_ENV_FILE` **必须指回自身**。
- 端口发布 `0.0.0.0:8800` → `validate()` **强制**要求非空 `WEB_ADMIN_TOKEN`，否则拒绝启动。
- 网络受限时构建走镜像站：`PYTHON_BASE=docker.1ms.run/library/python:3.13-slim`、
  `PIP_INDEX_URL`/`UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple/`（`registry-1.docker.io` 可能不可达）。
- `restart: unless-stopped` + Docker 开机自启 → 主机重启后容器自动恢复（已验证）。

---

## 8. 按模式的巡检与告警处置

### 巡检（三种模式通用，落 `can_buy` 一个词）
```bash
curl -fsS http://127.0.0.1:8800/api/metrics \
| python3 -c "import sys,json;h=json.load(sys.stdin)['health'];print(h['status'],h['can_buy'],h['buy_block_reason'],h['kill_switch']['armed'])"
```
`can_buy=false` → 看 `buy_block_reason` 定位（急停 / 对账 / 行情 / 熔断 / 关键任务 / 停机）。

### 告警处置

| 现象 | A 纸面 | B 测试网 | C 主网 |
|------|--------|----------|--------|
| 启动被拦 BLOCKED | 修配置后重启 | 核对四条件 | 逐条看 READINESS reasons，**不 bypass** |
| `status=KILLED` | 查原因 → `recover` | 查根因后 `recover` | **先 `stop` 冻结 → 人工核对交易所 vs 本地 → 收敛 → 再 `recover`** |
| 对账漂移超限 | 查 RPC/数据源 | 查测试网差异 | **宁停勿猜**：急停 + 停机 + 人工查证 |
| `ws_silence_seconds` 飙高 | 等行情恢复 | 同左 | 同左；持续则停机 |
| `recovery_streak` 告警 | 查底层反复异常 | 同左 | 同左 |
| 怀疑 key 泄漏 | — | 换测试网 key | **立即吊销 → 换新 key → 改 env → 重启** |

**通用原则：宁停勿猜。** 任何「看不懂的差异」→ 急停 + 停机 + 人工查证，
**绝不在不确定时继续自动交易**。

---

## 9. 模式切换检查清单

从 A → B（开真实执行）：
1. 测试网 key/secret 已配置且权限为只读+交易。
2. `PAPER_TRADING=false`、`RUN_TESTNET_TRADING=1`、`LIVE_TRADING_CONFIRM=` 留空。
3. 重启后确认 `=== TESTNET PREFLIGHT ===` 无 BLOCKED 且 `credentials_present=true`。
4. 先跑 `--paper` soak 确认稳定，再跑真实 soak。

从 B → C（开主网）：
1. 完成 [mainnet-readiness.md](mainnet-readiness.md) 人工 go/no-go 复审（含 L3 达成）。
2. 主网 key（Spot only、关提现、关资金转移）与测试网 key **物理分离**。
3. `BINANCE_TESTNET=false` + `LIVE_TRADING_CONFIRM=true` + `MAINNET_API_SCOPE_CONFIRM=true`。
4. 填入经书面批准的保守风控上限（不得沿用大额默认值）。
5. 重启后确认 `=== MAINNET READINESS ===` 九项全绿。
6. 首次只读接管通过 + 观察 ≥24h 不下单 → 再放开极小资金。

**回退（C → B/A）**：把 `BINANCE_TESTNET=true`（或 `PAPER_TRADING=true`）改回、
清空主网凭证、重启。急停态会持久化，**回退不会自动解冻**，需人工确认后 `recover`。

---

## 10. 诚实边界与冻结红线

**本文档不声称**：
- 主网已上线或已批准 —— 主网仍需人工 go/no-go，**自动化不得自行批准首笔主网交易**。
- 测试网 soak 已通过 —— 7h/24h 真实 soak 属部署验证项，状态见
  [testnet-operation.md](testnet-operation.md)。
- 纸面/测试网结果可外推主网成交质量。

**冻结红线（违反即回滚）**：扩资金扩币种 / Futures / 杠杆 / HFT / 复杂新指标 /
Transformer·RL / LLM 自动下单 / AI 绕过 TradingGate / 关掉对账或急停 / 忽略财务不变量 /
把「无报错」当「通过」。
