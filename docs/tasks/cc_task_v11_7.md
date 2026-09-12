# adaptiveTrading V11.7 — Testnet Evidence & Operational Hardening(任务清单 + 执行记录)

> 目标不是开发新策略, 而是把 V11.6 的「可运行基础设施」升级成「可验证、可审计、可复现的
> Testnet Operational Evidence」。产品边界继续冻结: Binance 单所 / SOLUSDT 单币 / Spot 现货 /
> 双仓 / 低频 / Python asyncio 单进程 / SQLite dev / MySQL prod / AI 只分析·提案、不直接下单。
> 禁止: 新币种 / Futures / HFT / RSI·MACD·BB 等新指标 / Transformer / RL / LLM 自动下单 /
> Kafka / K8s / 微服务 / 大型监控平台。
>
> 每单元完成即提交推送 main; 分支固定 main; `.env` 密钥绝不外泄; `logs/` 继续 gitignore。

---

## 0. 状态模型(P0-1: 重定义 Testnet / Soak 状态)

「代码已经实现」与「真实环境已经执行」必须严格区分, 不再用简单「✅」模糊。统一状态词汇表:

```text
IMPLEMENTED     代码/测试已落地并钉死(但未在真实环境执行)
READY_TO_RUN    实现完成 + 前置条件满足, 可执行但尚未执行
EXECUTED        已在真实环境实际运行
PASSED          真实执行且验收通过
FAILED          真实执行但未通过验收
BLOCKED         真实执行被环境/前置条件阻断(非代码缺陷)
NOT_EXECUTED    尚未在真实环境执行
```

八个跟踪项的实际状态(截至本版本收口):

| 项 | 代码态 | 真实执行态 | 证据锚点 |
|----|--------|-----------|----------|
| Testnet connectivity | IMPLEMENTED | **EXECUTED → PASSED** | `ping`/`time`/`exchangeInfo`/`account` 均返回(真实 testnet.binance.vision) |
| Testnet order lifecycle(no-fill) | IMPLEMENTED | **EXECUTED → PASSED** | LIMIT BUY no-fill → NEW → cancel → CANCELED, myTrades 空, SOL 余额不变 |
| real BUY | IMPLEMENTED | **EXECUTED → PASSED** | MARKET BUY 0.072 SOL @ 103.4 FILLED → Position/PositionLot/Order/OrderFill 落库 |
| real SELL | IMPLEMENTED | **EXECUTED → PASSED** | MARKET SELL 0.072 SOL @ 103.39 FILLED → 平仓 → SellAllocation 落库 → 持仓归零 |
| financial reconciliation | IMPLEMENTED | **EXECUTED → PASSED** | 单笔交易所真相 SOL 余额交叉对账(买入净增 ≈ fill_qty, 卖出回基线) |
| 7h soak | READY_TO_RUN | **NOT_EXECUTED** | 需真实 7h 无人值守运行(见 §P1-8) |
| 24h soak | READY_TO_RUN | **NOT_EXECUTED** | 需真实 24h 无人值守运行(见 §P1-8) |
| L3 readiness | — | **NOT_EXECUTED(仍 L2)** | 见 §P1-9: 单次 BUY/SELL 不升 L3, 需 7h soak PASS |

---

## 1. P0-2 — soak runner graceful shutdown(完成)

`at01_common/soak.py` 停机阶梯从裸 `proc.terminate()` 改为:

```text
正常完成 → POST /api/shutdown → 等待 graceful shutdown → 确认进程退出
         → 超时 terminate → 再次超时 kill
```

- shutdown 请求失败不崩溃; 进程已退出安全处理; KeyboardInterrupt 安全处理;
  terminate/kill 有明确 evidence; 不修改交易语义。
- 测试 `tests/unit/test_v167_soak_shutdown.py`(12 条)。

## 2. P0-3 — Soak Acceptance Contract(完成)

`evaluate_soak_result(...)` 纯逻辑验收, 输出 `PASS / FAIL / BLOCKED`; 检查 requested/actual
duration、最小采样数、进程正常退出、critical task failure、unexpected KILL、reconciliation
failure、runtime exception、final state、can_buy、kill switch、task failure count。
「can_buy 曾经 false」**不**直接视为失败(DEGRADED→can_buy=false 可能是正确安全行为);
关键是「是否违反预期安全契约」。
测试 `tests/unit/test_v168_soak_acceptance.py`(13 条)。

## 3. P0-4 — Runtime Evidence 可复现元数据(完成)

soak evidence 回答「这份证据是哪一版代码、什么配置、什么环境产生」。新增 run_id / git_sha
(真实 `git rev-parse HEAD`, 不硬编码)/ start_time / end_time / requested_duration /
actual_duration / symbol / paper_trading / binance_testnet / final_state / acceptance_result。
目录 `logs/soak/<run_id>/{metadata.json,evidence.jsonl,summary.json,runtime.log}`(logs/ 继续 gitignore)。
测试 `tests/unit/test_v170_soak_metadata.py`(9 条)。

## 4. P1-1 — Migration checksum(完成)

`at01_common/migrations.py` 在 `schema_version` 簿记表加 `checksum`(SHA-256); 首次 apply 记
checksum, 之后 `same version + same checksum → OK`、`same version + different checksum → FAIL FAST`
(RuntimeError「内容已变更」), 禁止静默接受已执行迁移被修改。
测试 `tests/unit/test_v169_db_migration_checksum.py`(6 条)。

## 5. P1-2 — Migration concurrency safety(完成)

最小化 migration lock: 进程内 `asyncio.Lock`(按 `asyncio.get_running_loop()` 惰性取锁, 避免
跨 loop 绑定); 跨进程由 `schema_version.version` PK 唯一约束兜底; lock 完成即释放; 不破坏启动。
测试 `tests/unit/test_v171_migration_concurrency.py`(2 条)。

## 6. P1-3 — Testnet evidence consistency(完成)

新建 `at01_common/evidence_chain.py`:
- `build_evidence_chain(...)`: 组装 `run_id → order → order_fill → position → position_lot →
  sell_allocation → exchange_truth → reconciliation → soak_result` 证据链 + `answers` 汇总;
- `chain_consistency_issues(...)`: 校验自洽(orphan_fill / fill_missing / fill_mismatch /
  buy_lot_mismatch / sell_alloc_mismatch / position_mismatch / reconciliation_failure /
  soak_result_invalid);
- `load_run_evidence(run_dir)`: 从 P0-4 的 metadata.json/summary.json 提取 run 级事实。
不新增数据库表, 复用已有表 + evidence 文件。
测试 `tests/unit/test_v172_evidence_chain.py`(16 条)。

## 7. P1-4 — Testnet real execution gate(完成)

新建 `at01_common/testnet_gate.py`: 真实(非纸面)执行须同时满足 `BINANCE_TESTNET=true` +
`PAPER_TRADING=false` + `RUN_TESTNET_TRADING=1` + `live_trading=false` + 测试网 key/secret 齐备,
否则 BLOCKED(拒绝启动)。输出 `=== TESTNET PREFLIGHT ===` 报告。接线到 `at01_common/wiring.py`
(主网守卫之后)。**绝对禁止主网误执行**。
测试 `tests/unit/test_v173_testnet_gate.py`(10 条)。

## 8. P1-5 — BUY 全路径复审(完成, 无 bypass)

重搜 `create_order` / `BUY` / `MARKET BUY` / `LIMIT BUY` / `place_order` / `submit_order`,
并核查反射/动态分发(getattr/importlib/eval/exec 无生产引用)。结论:

- `create_order` 仅存在于 `at50_execution/execution_executor.py`(paper/rest 两处), 无其它调用方;
- `ExecutionEngine.execute()` 是唯一下单咽喉, 仅 `run.py::_on_signal`(BUY, 闸门在 254 行)与
  `run.py::_apply_core_action`(ADD, 闸门在 913 行)两处调用;
- Web 无 BUY/SELL 下单端点(仅 /api/orders 读 + admin 急停/恢复/停机); AI 只写 ai_advices 表、
  无下单路径; recovery/startup/ledger_reconstruction 仅 DB `session.execute`, 不下单。
- 链路 `Signal → Strategy → Risk → TradingGate → Execution → Exchange` 完整, 无绕过。
- 既有 `test_v151` 集成测试已锁 `_on_signal`/`_apply_core_action` 均经闸门。

**结论: 未发现 bypass, 无需 P0 修复。**

## 9. P1-6 — Runtime health 与 evidence 一致性(完成)

锁定 `health.can_buy == TradingGate.can_open_position()[0]` 且 `health.can_sell ==
TradingGate.can_reduce_position()[0]`; health 显示 can_buy=true 而闸门实际禁止 → 严重缺陷。
`runtime_health.py` 已收口(单一权威); 补参数化测试逐维阻断(停机 / 关键任务 / 急停 / 降级 /
行情不健康 / 交易所不健康 / 对账未通过 / 资金熔断 REDUCE_ONLY·PAUSE)证明任一维度阻断时
health 与闸门仍逐字一致、绝不虚报「可买」。
测试 `tests/unit/test_v174_runtime_health_evidence_consistency.py`(12 条)。

## 10. P1-7 — 真实 Testnet preflight(EXECUTED → PASSED)

> 本会话测试网可达(`ping`/`time` 返回 200), 真实执行了测试网订单生命周期验证。

执行 `RUN_TESTNET_TRADING=1 pytest tests/testnet/test_v152_testnet_order_lifecycle.py`:

| 步骤 | 结果 |
|------|------|
| connect / ping / exchangeInfo / account / balances | ✅ 真实 `testnet.binance.vision` |
| LIMIT BUY no-fill → query NEW → cancel → verify CANCELED | ✅ PASSED, myTrades 空, SOL 余额不变 |
| MARKET BUY → verify fill → local accounting → exchange truth | ✅ PASSED, 0.072 SOL @ 103.4 FILLED, Position/PositionLot/Order/OrderFill 落库 |
| MARKET SELL → verify fill → close lot → sell allocation → reconciliation | ✅ PASSED, 0.072 SOL @ 103.39, SellAllocation 落库, 持仓归零, SOL 回基线 |

三重主网守卫(配置 `BINANCE_TESTNET=true` + 客户端 `testnet=True` + base_url 含 "testnet")全程在位;
`.env` 无主网 key。证据已由该 opt-in 测试自证(2 passed in 27.37s)。

## 11. P1-8 — 7h / 24h soak(NOT_EXECUTED)

前置条件(真实 order lifecycle)已满足(P1-7 PASSED), 但 7h / 24h 无人值守 soak 需真实挂机
7/24 小时, 本会话未执行。状态: **READY_TO_RUN → NOT_EXECUTED**。
续跑步骤见 [testnet-operation.md](../testnet-operation.md) §4(soak 用 .env 已列, 命令
`python -m at01_common.soak --hours 7`)。**不因网络/时长不可达而伪造结果。**

## 12. P1-9 — 最终 readiness 严格判定(仍 L2)

规则: 没有真实 Testnet execution → L2; 只有一次 BUY/SELL → 仍 L2; 真实 7h soak PASS → L3
candidate; 真实 24h soak PASS + evidence 完整 + reconciliation PASS + recovery PASS + security
PASS → L3; L4 另行主网 readiness review(绝不自动推导)。

本会话达成**一次真实 BUY/SELL 闭环(P1-7 PASSED)**, 按规则**仍为 L2(运行时验证就绪)**,
不虚报 L3。L3 需 P1-8 的真实 7h/24h soak 在部署环境跑通后评估。

---

## 13. 验证(十七)

- **pytest**: `pytest tests/ -q --cov --cov-report=term-missing --cov-fail-under=75 -m "not testnet"`
  → **1219 passed, 6 deselected**; coverage **79.45% ≥ 75%**。
- **ruff**: `ruff check .` → All checks passed。
- **mypy**: 本次未改动核心类型(迁移/证据链/闸门均为新增纯逻辑或薄接线), 未强制重跑
  (V11.5 起 mypy 为建议性检查, 非 CI 门禁)。
- **testnet**: `pytest tests/testnet -m testnet` 需显式环境变量, CI 永不触发真实交易
  (`-m "not testnet"` 排除)。

## 14. 实际交付清单

**新增模块**:
- `at01_common/evidence_chain.py`(P1-3 证据链)
- `at01_common/testnet_gate.py`(P1-4 测试网真实执行闸门)

**修改模块**:
- `at01_common/soak.py`(P0-2 优雅停机 + P0-4 可复现元数据)
- `at01_common/migrations.py`(P1-1 checksum + P1-2 并发锁)
- `at01_common/wiring.py`(P1-4 接线 preflight)

**新增测试**(80 条, test_v167~v174):
- `test_v167_soak_shutdown.py`(12)/ `test_v168_soak_acceptance.py`(13)/
  `test_v169_db_migration_checksum.py`(6)/ `test_v170_soak_metadata.py`(9)/
  `test_v171_migration_concurrency.py`(2)/ `test_v172_evidence_chain.py`(16)/
  `test_v173_testnet_gate.py`(10)/ `test_v174_runtime_health_evidence_consistency.py`(12)。

**无新表无迁移**(迁移框架本身加 checksum 列 + 并发锁, 不影响领域 schema)。

## 15. 诚实披露

- **7h/24h soak 未执行(NOT_EXECUTED)**: 需真实挂机 7/24 小时, 非代码缺陷; 不伪造。
- **L3 未达**: 仅一次真实 BUY/SELL, 按规则仍 L2; 不因网络恢复而虚报 L3。
- **financial reconciliation 仅单笔级**: 单笔 BUY/SELL 的交易所真相交叉对账已 PASS;
  周期级权益对账 + 交叉对账矩阵(随 soak 运行)仍属 NOT_EXECUTED(随 P1-8)。
- **账户余额**: 真实成交 0.072 SOL(≈ $7.4 等价测试网币, 无真实价值), 余额足够、未触发跳过。
