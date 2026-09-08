# adaptiveTrading V11.4 — Real Runtime Validation

> 定位: V11.3 已完成「生产加固 / 长跑可靠 / 配置正确 / 文档真实」。但 **「测试证明正确」≠「长跑证明正确」**,
> 且不信任 V11.3 文档自述, 从当前 `main` 重新审计。V11.4 不再开发新模块, 而是:
> **Long-Running Runtime Validation / Final Security Review / Product-Freeze Re-Verification**。
>
> 原则: 禁止新增交易策略(RSI/MACD/Bollinger/Transformer/RL/AI 自动下单/新币种/Futures/高频)。
> 冻结不变: Binance 单所 / SOLUSDT 单币 / 现货 / 双仓 / 低频; AI 只分析优化、不直接下单。
> 每单元完成即提交推送 main。

## 当前状态(每单元更新)

- 版本: V11.4(完成)
- 测试: 1032/1032 通过(+4 真实测试网冒烟 opt-in 排除于 CI)
- 覆盖率: 78.95%(阈值 75%)
- ruff: 全绿(E9 + F, 忽略 F401)
- 分支: main
- 就绪等级: L2(运行时验证就绪)

## P0 — 运行时验证(长跑证明正确)

| # | 任务 | 状态 |
|---|------|------|
| P0-2 | 长跑 Soak 测试(24h/72h/对账/恢复)+ 故障注入财务不变量 | ✅ |
| P0-3 | 证明「任何异常态下绝不继续 BUY」 | ✅ |
| P0-4 | Recovery 状态机穷举审计(全迁移矩阵) | ✅ |
| P0-5 | 崩溃 / 重启 Soak(急停持久化 + 状态收敛) | ✅ |
| P0-6 | 真实币安测试网只读冒烟(不下单) | ✅ |
| P0-7 | 数据库迁移设计审计(schema 锚点) | ✅ |
| P0-8 | run.py 运行时监督审计(局部导入死路径) | ✅ |

## P1 — 生产就绪 / 交付收口

| # | 任务 | 状态 |
|---|------|------|
| P1-1 | CI 加强(ruff 正确性基线 + coverage 阈值) | ✅ |
| P1-2 | Testnet Smoke 与 CI 隔离 | ✅ |
| P1-3 | 生产就绪等级更新(L1~L4, 当前 L2) | ✅ |
| P1-4 | 运行报告机制(每日复盘附运行状态快照) | ✅ |
| P1-5 | Observability 最终检查(消除死指标) | ✅ |
| P1-6 | Static Audit(TODO/FIXME/bare-except 零) | ✅ |
| P1-7 | 文档同步(强制) | ✅ |
| P1-8 | 测试执行(最终全量确认) | ✅ |
| P1-9 | 最终安全审查(密钥/危险调用/Web 面板) | ✅ |
| P1-10 | 不要扩展产品(冻结复核) | ✅ |
| P1-11 | 保存项目进度(progress.md + memory) | ✅ |
| P1-12 | CC 连续性文档(本文档) | ✅ |
| P1-13 | Commit + Push(最终收口) | ✅ |

---

### P0-2 长跑 Soak 测试 ✅(2026-09-08)

**定位**: 把「单次单元测试正确」升级为「压缩长跑周期逐周期正确」—— 在缩短的周期内反复跑交易链路并注入
故障, 每个周期末断言财务不变量, 用「不变量在数百个周期内恒成立」证明长跑不会累积漂移。

**交付**(`tests/long_running/`):

- `test_24h_soak.py`(V11.4 P0-2): 24 个压缩交易周期(简单 + 多 lot)+ 6 类故障注入
  (WS 断开 / REST 超时 / 重复成交 / 部分成交 / 部分成交后撤单 / 未知订单), 逐周期断言 5 条财务不变量。
- `test_72h_soak.py`(V11.4 P0-2): 72 个压缩周期 + DB / 手续费 / 资金漂移故障注入。
- `test_reconciliation_soak.py`(V11.4 P0-2): 对账长跑仿真(对账不一致 / 漂移分级 / 对账后恢复交易)。
- `test_recovery_soak.py`(V11.4 P0-2): 恢复/重启长跑仿真(kill switch / recovery / restart / recovery-then-trade)。

**核心不变量**(逐周期断言): Base 守恒 / Lots 守恒 / SellAllocation 守恒 / Cash 守恒(含手续费)/ Equity 守恒;
故障后最终态(PASS/DEGRADED/REDUCE_ONLY/RECOVERY/KILLED)明确且一致, **绝不「异常 → 继续 BUY」**。

---

### P0-3 证明异常绝不继续 BUY ✅(2026-09-08)

**交付**: `tests/unit/test_v142_abnormal_never_buy.py`。

**核心不变量**: 系统进入任何异常态(急停 / 熔断 / 暂停 / 仅减仓 / 急停态 / 恢复核验 / 价格尖刺 /
快速暴跌 / 行情静默 / 连续执行失败), 都必须且实际做到「不再下达任何 BUY 新仓」。

**方法**: 真实风控 `RiskManager` + 统一闸门 `TradingGate` 装配(与 run.py 一致), 逐异常态注入后断言
`can_open_position()` 为 False, 外加静态源码断言(run.py 开仓入口只走 `TradingGate.can_open_position`,
防绕过)。

---

### P0-4 Recovery 状态机穷举审计 ✅(2026-09-08)

**交付**: `tests/unit/test_v143_recovery_state_machine_audit.py`。

**审计目标**: 把 `RiskStateMachine` 的 NORMAL / REDUCE_ONLY / PAUSED / KILLED / RECOVERY_CHECK 五态
**全迁移矩阵 + 方向闸门 + 恢复两步语义**一次性钉死, 防止未来改动引入「某异常态可逃逸到可交易」的回归。

---

### P0-5 崩溃 / 重启 Soak ✅(2026-09-08)

**交付**: `tests/long_running/test_crash_restart_soak.py`。

**验证**: 急停(KillSwitch)持久化到 `kill_switch_state`(单行 id=1); 重启后新 `RiskManager` 经
`_load_kill_switch` 恢复 armed 状态, **绝不自动复位**; 崩溃/重启后安全状态收敛且不漂移。

---

### P0-6 真实测试网只读冒烟 ✅(2026-09-08)

**交付**: `tests/smoke/test_testnet_smoke.py`。

**范围**: 连通 / 交易规则(ExchangeInfo)/ 行情 / 鉴权(测试网 key)—— 只读, **不下单**。真实
`https://testnet.binance.vision` REST + `wss://stream.testnet.binance.vision/ws`。CI 显式排除
(`-m "not testnet"`), 仅 `RUN_TESTNET_SMOKE=1` 时 opt-in 执行。

---

### P0-7 数据库迁移设计审计 ✅(2026-09-08)

**审计结论**: 无 Alembic; `create_all` 只建缺失表、不 ALTER 已有表。以「schema 稳定性锚点」兜底:
`test_v129_schema_audit.py` 钉死 25 表清单 + 259 列全列清单, 任何 create_all 静默列漂移都会被测试显式暴露。

---

### P0-8 run.py 运行时监督审计 ✅(2026-09-08)

**审查结论**: `AdaptiveTradingSystem.initialize()` 内**局部导入**的若干观测函数与枚举
(`evaluate_alerts` / `record_execution` / `record_reconcile_verdict` / `record_breaker_action` /
`CoreAction`)在其它方法里被引用, 但那些方法无自己的局部导入、也不在模块级导入 → 运行期 `NameError`
死路径(熔断决策 / 对账 KILL / 双仓 / core ADD·REDUCE / 告警)。

**交付**: `tests/unit/test_v130_run_supervisor.py`; 修复 6 处局部导入 NameError 死路径。

---

### P1-1 CI 加强 ✅(2026-09-08)

**交付**: `pyproject.toml` 增 `ruff>=0.5.0` + `[tool.ruff]`(E9+F, 忽略 F401)+ `[tool.coverage.run/report]`
(`fail_under=75`, source = 10 个 atXX 包); `.github/workflows/ci.yml` 增 Lint(ruff)步骤 + coverage 步骤
(`-m "not testnet" --cov-fail-under=75`)。

---

### P1-2 Testnet Smoke 与 CI 隔离 ✅(2026-09-08)

**交付**: CI 显式 `-m "not testnet"` 排除 + fixture 未设 `RUN_TESTNET_SMOKE` 时 skip(双保险),
真实测试网冒烟绝不进 CI 主路径。

---

### P1-3 生产就绪等级更新 ✅(2026-09-08)

**交付**: `docs/production-readiness.md` 引入 L1~L4 就绪等级表, 当前 **L2(运行时验证就绪)**。
L3(测试网无人值守实盘)属运维部署验证, 不在代码交付内。

---

### P1-4 运行报告机制 ✅(2026-09-08)

**交付**: `at40_journal/daily_report.py` 新增 `_render_health`(渲染生命周期 / 风险态 / 急停 / 熔断 / 告警);
`run.py` 新增 `_runtime_health()` 快照装配 + `_daily_report_loop` 传入 health。
**回归测试 7 条**(`test_v146_runtime_report.py`): 运行状态渲染 4 + 装配 3。

---

### P1-5 Observability 最终检查 ✅(2026-09-08)

**审查结论**: 两类死指标(采集与观测脱节)。

- 「写而不读」: `orders_unknown` / `recovery_required` / `reconcile_{degraded,recovery_required,killed}` /
  `breaker_{reduce_only,pause,kill}` 被 `record_*` 采集但从不进 `snapshot()` → 补入 `snapshot()`。
- 「读而不写」: `data_gaps` 计数从不被写 → `RiskManager.silence_active` 新增属性 + `_risk_loop` 上升沿
  检测接线。
**回归测试 4 条**(`test_v147_observability_final.py`): snapshot 无死写 + silence_active 转换。

---

### P1-6 Static Audit ✅(2026-09-08)

**结论**: 全量扫描 TODO/FIXME/bare-except 零; 2 处 `except Exception: pass` 加注释说明
(`database.py` reset dispose 尽力而为 / `market_engine.py` Redis pub-sub 可选旁路)。

---

### P1-7 文档同步 ✅(2026-09-08)

**交付**: README/progress/architecture 版本 V11.3→V11.4; module-map 测试数 885→1032 并补 V11.3/V11.4
测试文件与头部里程碑摘要; production-readiness/runbook 测试数 1021→1032、覆盖率 78.5%→78.9%。

---

### P1-8 测试执行 ✅(2026-09-08)

**最终确认**: `.venv\Scripts\python -m pytest tests/ -q --cov --cov-report=term-missing --cov-fail-under=75 -m "not testnet"`
→ **1032 passed, 4 deselected**(169.65s); 覆盖率 **78.95%** ≥ 75%; `ruff check .` 全绿。

---

### P1-9 最终安全审查 ✅(2026-09-08)

**结论**:

- `.env` 已 gitignore(仅 `.env.example` 占位符入库); 仓库与 git 历史**无硬编码密钥**(BINANCE/AI key 均无)。
- 生产源码**无** `eval`/`exec`/`pickle.loads`/`os.system`/`subprocess`/`__import__` 危险调用。
- 主网守卫(`mainnet_blocked_reason`)+ 纸面默认 + `Settings.validate()` fail-fast 在位。
- **唯一披露项**: Web 面板默认 `API_HOST=0.0.0.0:8800` 且无鉴权(含 `POST /api/breaker/reset` 写接口),
  已列入 production-readiness §8 已知限制(单机设 `API_HOST=127.0.0.1` / 局域网/公网加反向代理+鉴权)。

---

### P1-10 不要扩展产品 ✅(2026-09-08)

**冻结复核通过**:

- 单所 Binance / 单币 SOLUSDT(`SUPPORTED_SYMBOLS=("SOLUSDT",)` 钉死 + 非 SOLUSDT fail-fast)。
- 现货: `market_futures_client.py` 仅只读资金费率 + 持仓量作情绪数据(`get_funding_rate` /
  `get_open_interest`, 无 `_post`/下单方法), **无合约下单路径**。
- 双仓 core/trade(`portfolio_core/trading/cash_ratio`); 低频(risk 5s / regime 30s)。
- AI 只提案: 优化器无 `.activate(` 调用方, `snapshot` 落库 `active=False`。
- 无 RSI / MACD / Bollinger / Transformer / RL / HFT; 策略文件仍为 base/buy/decision/engine/grid/sell/trend 七件套。

---

### P1-11 保存项目进度 ✅(2026-09-08)

**交付**: `docs/progress.md` 补 V11.4 P1-7..P1-10 交付结论; memory 更新为 `v114-project-state`
(V11.4 完成态, 1032 测试, L2, 含 CI 门槛 + Web 面板已知限制)。

---

### P1-12 CC 连续性文档 ✅(2026-09-08)

**交付**: 本文档(`cc_task_v11_4.md`), 记录 V11.4 P0/P1 交付与审计记录, 供下一切片续接。

---

### P1-13 Commit + Push ✅(2026-09-08)

**交付**: 逐单元提交推送 main; 最终收口提交见 git log。

---

**V11.4 全部完成**: P0(7) + P1(13) 共 20 单元, 全量 **1032/1032** 通过(+4 testnet 冒烟 opt-in),
覆盖率 78.95%, ruff 全绿, main 分支逐单元提交推送。就绪等级 **L2**, 下一步 L3 属运维部署验证。
