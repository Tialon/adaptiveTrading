# CC 执行任务 — 配置数据库化 / 守卫降摩擦 / 死开关清理 / Pi 漂移根因修复（2026-09-12）

> 背景：操作者诉求是「模式切换与运行参数写数据库、切换实盘别那么多验证、提高易用性」。
> 经排查，其中「去掉运行时闸门」一项**不予实现**（理由见 §0），但**其余诉求全部采纳**，
> 且排查过程中定位到一个**把测试网永久锁死的真实 bug**（P0）。

---

## 0. 范围裁定：为什么不拆运行时闸门

操作者要求「切换实盘无需卫士/闸门等繁多验证」。**必须区分两类东西**：

| | A. 启动前守卫 | B. 运行时闸门 |
|---|---|---|
| 组件 | `mainnet_blocked_reason()` / `mainnet_readiness_check()` 九项 / `testnet_gate` / `validate()` 主网部分 | `TradingGate` 六维+两维 |
| 何时跑 | 启动一次 | **每一笔单** |
| 处置 | **P2 降摩擦**（显式解锁，留痕+过期+横幅） | **保持原样，不移除** |

**不移除 B 的依据（Pi 实测，非推测）**：

```
health.status      = KILLED
kill_switch.armed  = true   原因「对账矩阵 KILLED: equity:equity_drift SOLUSDT」
lifecycle          = SAFE_MODE  「资金熔断: equity=KILL, position=PAUSE, cash=PAUSE」
orders_total       = 0       从未成交
reconcile_killed   = 81      连续 81 次 KILL
```

移除 B 不会让系统「能交易」，只会让它**拿着一个被证明错 100% 的账本去下真钱单**
（用错误权益算仓位、对不存在的持仓下单）。**A 是摩擦，B 是安全网，不可混为一谈。**

---

## P0：Pi `equity_drift=1.0` 根因修复（最高优先级）

### 已定位的根因

`at50_risk/risk_manager.py:120`

```python
def current_equity(self) -> float:
    return self.breaker.current_equity or self.settings.risk_initial_equity
```

`risk_initial_equity` 默认 **100000.0**（`settings.py:164`）—— 一个**配置默认值**。

而 `at01_common/wiring.py:279-284` 中「从真实账户播种权益基线」的接管逻辑：

```python
if (not system.settings.paper_trading
    and not system.settings.binance_testnet      # ← 仅主网
    and system.settings.mainnet_takeover_enabled
    and not await system.hodl_benchmark.has_baseline()):
```

**只在主网执行。** 于是在 `live_testnet`：

1. 本地权益 = `risk_initial_equity` = 100000（配置默认，**从未与真实账户对齐**）
2. 交易所测试网账户权益 = 实际余额
3. `reconcile_account` → `|local - exchange| / local` ≈ **100%** > 容差 2%
4. `equity_drift` 在 `reconciliation_matrix.py` 属 `_KILLED_ALONE`（单发即 KILL，无需佐证）
5. → KILLED → SAFE_MODE → `orders_total=0` → 无成交 → 漂移永远不收敛 → **81 次连续 KILL**

**这是假阳性，不是真实资金风险**：市场健康（`data_healthy=true`）、交易所健康
（`exchange.healthy=true`）、8 个任务全在跑、`risk.state=NORMAL`，唯一异常就是这一条。

### 修复要求

1. **实盘（任何非纸面模式）启动时都要播种权益基线**，不限于主网。
   - 抽出「从交易所账户取权益并作为基线」这一步，让 `live_testnet` 也走
   - **主网专属的 go/no-go 门保留**（意外挂单/持仓漂移 → 冻结），那是真实保护，不动
   - 基线须**持久化**（复用 `hodl_benchmark` 既有基线记录），重启不重复播种
2. **不得放宽 `equity_drift` 的 KILLED 判定** —— 修的是「基线错了」，不是「别报漂移」。
   修好之后若仍有漂移，**必须照常 KILL**。
3. 基线播种失败（API 不通 / 账户读取失败）→ **保持原有 BLOCKED 行为**，不得静默放行。
4. 补测试：
   - `live_testnet` 启动后 `current_equity` 等于交易所账户权益（而非 100000）
   - 播种后本地 vs 交易所漂移 < 容差 → 不产生 `equity_drift`
   - 播种失败 → 不 arm、但也不放行交易（保持 fail-closed）
   - **真实漂移仍然 KILL**（防止修复把判定改软）

### 验收

- 在 Pi 上重启后，`/api/metrics` 的 `reconcile_drift_pct` < `EQUITY_RECONCILE_TOLERANCE_PCT`
- `kill_switch.armed` 需人工 `recover` 解除（**不自动解冻**，语义不变）
- 解除后 `can_buy` 由 `TradingGate` 正常判定，**不人为置 true**

---

## P1：运行参数写入数据库

### 目标

`/admin` 的配置编辑从「写 env 文件」升级为「写数据库」，使 Pi 上改配置**不再需要**
挂载可写配置目录（当前需 `chown -R 999:999 /etc/adaptive-trading`）。

### 设计

新增两张表（`create_all` 自动建，无需 ALTER）：

| 表 | 字段 |
|---|---|
| `runtime_config` | `key`(PK) / `value` / `updated_at` / `updated_by` / `reason` |
| `runtime_config_history` | `id` / `key` / `old_value` / `new_value` / `changed_at` / `changed_by` / `reason` |

**优先级：DB > env > default**（操作者已确认）。

**三条硬边界**：

1. **密钥永不入库** —— 复用 `config_store.py` 现有 `is_secret` 判定
   （含 `KEY`/`SECRET`/`TOKEN`/`PASSWORD` 的字段一律 env-only、只读、不回显）。
   理由：DB 会被备份、被拷来拷去，密钥一旦入库就等于扩散。
2. **白名单** —— 只有 allowlist 可入库。**排除 bootstrap 关键项**
   （`DATABASE_URL` / `API_HOST` / `API_PORT` / `WEB_ADMIN_TOKEN` 等）——
   它们必须在数据库连上**之前**就有效，存在自己指向的库里是循环依赖。
3. **分两类**（复用 `FieldSpec` 已有的 `restart_required` 标记）：
   - **热生效**：每次读取时取值的参数（如风控阈值）
   - **需重启**：构造期读取的参数（如指标窗口）—— 页面必须**明确告知**，
     不得让操作者误以为一定即时生效

### 实现要求

- 新模块 `at01_common/runtime_config.py`：加载 / 保存 / 校验 / 审计
- **启动顺序**：DB 配置必须在 `settings.validate()` 与守卫链**之前**应用，
  否则「改了配置但校验用的是旧值」。需调整 `wiring.py::wire_system()` 的顺序，
  并确保 DB 未就绪时**优雅降级到 env**（不得因此启动失败）
- `/api/admin/config` 返回每个字段的**生效来源**（`db` / `env` / `default`）
- `/api/admin/config/apply`：DB 字段写 DB，env-only 字段维持原文件写入路径
- 每次变更写 `runtime_config_history`（**改风控阈值 = 改交易行为，必须可追溯**）

### 验收

- Pi 上不挂载可写配置目录也能保存配置
- 密钥类字段在 DB 中**不存在任何行**（补测试直接断言）
- 改一个热生效参数 → 不重启即生效；改一个需重启参数 → 页面明确提示
- DB 不可用 → 回落到 env，启动照常

---

## P2：启动守卫显式解锁（降摩擦，不移除）

### 目标

把「切模式要修 8 个配置文件 + 读半天日志」变成「选模式 → 显式确认一次 → 完成」。

### 设计

新增 `at01_common/guard_override.py`：

```
GUARD_OVERRIDE=<ISO8601 到期时间>:<确认短语>
例: GUARD_OVERRIDE=2026-09-13T00:00:00Z:I-KNOW-THIS-IS-MAINNET
```

生效条件（**全部满足**才放行）：

1. 格式正确且短语**逐字**匹配
2. **未过期**（超过到期时间自动失效）
3. 解析成功

生效时的**强制留痕**（这是防手滑的关键，不可省）：

- 启动日志打**醒目横幅**（每次启动都打，不是只打一次）
- `/` `/admin` `/ops` **常驻红条**（复用现有 `auth-notice` 机制）
- 写一条 `risk_events`（`event_type='guard_override'`）
- `/api/operator-status` 增加 `guard_override: {active, expires_at, reason}` 字段，
  **面板必须显示**，不得隐藏

### 硬约束

- **默认关闭**，不设 `GUARD_OVERRIDE` 时行为与现在**逐字一致**（现有主网测试必须全绿）
- **只解锁启动前守卫（A 类）**。`TradingGate`（B 类）**不受其影响**
- 不得提供任何「永久关闭」形态 —— 必须带到期时间

### 验收

- 不设该变量 → 主网仍被拦（现有测试不变）
- 设置正确 → 放行且 `/api/operator-status.guard_override.active == true`
- 过期 / 短语错 / 格式错 → 一律**照常拦截**（fail-closed）
- 补测试覆盖上述四种情形

---

## P3：死开关清理（已核实）

代码检查发现的「声明了但从不生效」的配置项，**逐条已用全仓库引用计数核实**：

| 字段 | 证据 | 处置 |
|---|---|---|
| `MAINNET_READINESS_ENABLED` | 仅出现在 `config_store.py:101` 的 FieldSpec，**全代码库从不被读取**；自检在 `wiring.py` 里只要 `BINANCE_TESTNET=false` 就**无条件执行** | ⚠️ **最坏的一种** —— 在 `/admin` 上能关，关了什么也没变，给人**虚假的掌控感**。从页面移除，如实说明 |
| `portfolio_profit_sweep_enabled` | 0 处引用 | 移除 |
| `database_pool_size` / `database_max_overflow` | 0 处引用（SQLite 下亦无意义） | 移除 |
| `regime_hmm_model_path` | 0 处引用 | 移除 |
| `regime_hmm_enabled` | 仅出现在 docstring | 确认后移除或接线 |

> 注：`analytics_vwap_window` / `analytics_whale_quantile` / `analytics_accumulation_window`
> 初筛误报 —— 它们经 `s.xxx` 前缀正常使用，**不是**死配置，不要动。

**补防回归测试**：断言 `config_store.FIELD_SPECS` 里的每个 key 在代码库中
**至少有一处真实读取**。防止以后再往管理页面塞空开关。

---

## P4：跳过（操作者指定略过 Web Tab 改造）

---

## P5：测试与文档

- 补测试：P0 四条 / P1 密钥不入库 + 优先级 + 降级 / P2 四种情形 / P3 防回归
- 更新文档：
  - `docs/operating-modes-manual.md`：补 `GUARD_OVERRIDE` 说明（含「B 类闸门不受影响」）
  - `docs/architecture.md`：补 `runtime_config` 表（表数 26 → 28）
  - `docs/database-migration.md`：登记两张新表 + `SCHEMA_VERSION` 递增
  - `docs/runbook.md`：补「配置数据库化后 Pi 不再需要可写配置目录」
  - `docs/progress.md`：记录 P0 根因与修复
- 同步锚点：`tests/unit/test_v129_schema_audit.py` 表清单 + `SCHEMA_VERSION`

最终验收：

```bash
uv run ruff check .
uv run mypy
uv run pytest -q --cov --cov-report=term-missing --cov-fail-under=75 -m "not testnet"
python scripts/check_docs_mermaid.py docs/
```

---

## 禁止项

- **不允许移除或放宽 `TradingGate`** 的任何一维。
- **不允许放宽 `mainnet_readiness_check()` 九项、`mainnet_blocked_reason()`、`testnet_gate`**
  的判定 —— P2 只提供**显式、留痕、会过期**的解锁通道，不改变默认行为。
- **不允许把 `equity_drift` 的 KILLED 判定改软** —— P0 修的是基线错误，不是漂移检测。
- **不允许密钥入库**或出现在任何返回体/日志/页面中。
- **不允许把「配置已保存」写成「已生效」** —— 需重启的必须明确说需重启。
- **不允许为了通过测试而修改断言语义**（例外：P3 移除死开关时同步删除对应断言，
  须在提交信息里说明）。
