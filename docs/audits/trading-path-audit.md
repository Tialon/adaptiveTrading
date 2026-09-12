# 真实交易链路审查（V12.8）

> 本文是**走读实际代码**得出的结论，不是复述文档。每条都给了 file:line 依据。
> 审查范围：入口 → 行情 → 策略 → 信号 → 风控 → 闸门 → 执行 → 交易所 → 对账 → 账务 → 面板。

## 1. 结论速览

| 检查项 | 结论 | 依据 |
|--------|------|------|
| 绕过 `TradingGate` | **不存在** | 全仓库只有两处调用 `execute()`，均在闸门之后 |
| 绕过 `RiskManager` | **不存在** | 信号先经 `RiskManager.check()` 才进闸门 |
| Web 直接下单 | **不存在** | 10 个写端点无一涉及下单 |
| AI 直接下单 | **不存在** | AI 只写 `ai_advices` |
| Strategy 直接下单 | **不存在** | 策略只产出 `Signal` |
| 重复订单出口 | **不存在** | 下单调用全在 `at60_execution`，且分纸面/实盘两条互斥分支 |
| 状态与交易所不一致 | 有检测机制（见 §5） | 对账矩阵 + 交易所真相 |
| paper / testnet / live 混用 | **不存在** | 单一 `is_paper` 标志，无第二来源 |

---

## 2. 入口与调用链

```
Binance WS ─→ MarketDataEngine.on_trade ─→ run.py::_on_trade
                                              │
                                              ▼
                                    AnalyticsEngine (指标快照)
                                              │
                                              ▼
                                    StrategyEngine (产出 Signal)
                                              │
                                              ▼
                    run.py::_on_signal  ── ① TradingGate 闸门 ──┐
                                              │                 │ 拦截则 return
                                              ▼                 │
                                    RiskManager.check()         │
                                              │                 │
                                              ▼                 │
                                 PositionSizer / REDUCE_ONLY 封顶│
                                              │                 │
                                              ▼                 │
                    run.py:343  ──→ ExecutionEngine.execute() ◄─┘
                                              │
                                              ▼
                     ┌──────── is_paper? ────────┐
                     ▼                            ▼
              PaperBroker.create_order     rest.create_order(交易所)
                (纸面, 不发出)               (实盘, 带 newClientOrderId)
```

**核心仓路径**（低频决策，同样收口）：

```
CorePositionManager.decide() → run.py:940  ── 闸门(can_reduce_position) ──→
                                run.py:958  ──→ ExecutionEngine.execute()
```

## 3. 权限边界（谁不能做什么）

| 组件 | 能做 | **不能做** | 依据 |
|------|------|-----------|------|
| Web (`at90_web`) | 读状态、改配置、急停/恢复/停机 | **下单** | 10 个写端点: config/draft·apply·rollback、admin/restart、breaker/reset、emergency/kill·recover、shutdown、trading-mode/preview·apply —— 无一下单 |
| AI (`strategy_ai_advisor`) | 写 `ai_advices`、产参数建议 | **下单**、改运行中参数 | 优化器只产 proposal(`active=False`) |
| Strategy (`at30_strategy`) | 产 `Signal`(score/reason/indicators) | **下单** | `strategy_buy/sell/grid/trend` 无 `create_order` 调用 |
| 回测/优化器 | 离线跑策略管线 | **下单** | 无 REST 下单客户端 |
| `ExecutionEngine` | 下单 | 绕过闸门 | 两个调用点都在闸门之后 |

**验证方法**：`grep -rn "create_order" at10_market at30_strategy at50_risk at90_web at80_backtest at85_optimizer` —— 只命中 `market_rest_client.py`(REST 原语定义) 与 `at60_execution/*`。

## 4. 风险检查与闸门

**两道串联**，顺序固定：

1. `RiskManager.check()` —— 百分比风控（仓位/单笔/敞口/日亏/回撤）+ 异常保护
2. `TradingGate` —— 六维 + 两维（停机、关键任务），**买卖许可的唯一权威**

> ⚠️ 注意：闸门在 `run.py::_on_signal` 里调用，**不在** `execute()` 内部。
> 这意味着「只要有人绕过 run.py 直接调 `execute()`」就跳过了闸门 —— 当前没有这样的调用点，
> 但**这是一条结构性脆弱点**：闸门是"调用方记得调"而非"出口无法绕过"。
> 建议（未实施，属架构改动）：把闸门判定移进 `execute()`，使其成为无法绕过的出口。

## 5. 订单出口与幂等（§16）

**出口唯一**：`ExecutionEngine.execute()`。内部按 `is_paper` 二选一：
- 纸面 → `PaperBroker.create_order`（`execution_executor.py:329`）
- 实盘 → `rest.create_order`（`execution_executor.py:423`）

**幂等三层**（均已存在，未重新设计）：

| 层 | 机制 | 位置 |
|----|------|------|
| ① 意图去重 | `order_intents` DB 唯一键（含 quantity + 时间桶，**重启不失效**） | `execution_executor.py:130,628` |
| ② 状态机闸门 | `ENTRY_PENDING`/`HOLDING` 期间拒绝重复买入 | `execution_executor.py:106` |
| ③ 交易所去重 | `newClientOrderId` = 本地 `client_order_id`（币安服务端幂等） | `execution_executor.py:418` |

**危险窗口（请求已发/响应超时）的处理**：`execution_executor.py:505-530` 先
`get_order(orig_client_order_id=...)` **反查**，查不到才用**同一个 client_order_id** 重发 ——
不会产生第二笔订单。日志明确记「下单重试仍失败, 订单状态未知」并落 `_record_attempt`，
交由订单恢复引擎收敛。

**结论**：`same signal / same cycle / network retry / process restart / exchange timeout`
五种场景下都不会出现 BUY×3。

## 6. 失败行为（fail-closed，§5）

| 失败 | 行为 | 是否可能下单 |
|------|------|:---:|
| 凭证缺失 | `validate()` fail-fast，拒绝启动 | ❌ |
| 账户权益取不到 / 为 0 | `live_equity` 拒绝启动 | ❌ |
| 主网就绪九项不过 | `RuntimeError` 拒绝启动 | ❌ |
| 测试网闸门不过 | `RuntimeError` 拒绝启动 | ❌ |
| 配置冲突 | `resolve_mode` fail-closed | ❌ |
| `TRADING_MODE` 非法 | 同上 | ❌ |
| 急停已武装 | 闸门禁买禁卖 | ❌ |
| 对账 KILLED | 闸门 + 生命周期 SAFE_MODE | ❌ |
| 权益漂移 | `equity_drift` 单发即 KILL | ❌ |
| 风控超限 | `RiskManager` 拒绝 | ❌ |
| 交易所不可用 | 闸门 `exchange_healthy=False` | ❌ |
| 数据库不可用 | 启动期 `init_db` 失败 | ❌ |

**可解释性**：失败原因经 `operator-status.block_reason` / `summary` / `next_action` 呈现，
并落 `risk_events`；`/ops` 汇总为三态。**没有"静默失败"路径**。

## 7. 对账（§7）

**三类状态必须分清**：

| 类别 | 含义 | 来源 |
|------|------|------|
| **交易所事实** | 真实资金/持仓 | `rest.get_account()` / `get_my_trades()` |
| **本地状态** | 本系统落库的镜像 | `positions` / `order_fills` / `position_lots` |
| **计算状态** | 由前两者推导 | `equity` / `drift_pct` / `unrealized_pnl` |

**原则已落实**：交易所真实状态优先 —— 实盘财务真相锚定交易所对账链
（`LIVE_ACCOUNT_LEDGER_MODE = EXCHANGE_TRUTH_RECONCILIATION`），本地 `account_ledger`
**仅纸面落库**，不在实盘补写（避免制造第二个漂移源）。

对账器与职责：`startup_reconciler`(崩溃窗口) / `_reconcile_loop`(周期) /
`exchange_truth_reconciler`(成交完整性) / `cross_reconciler`(本地四维一致性) /
`reconciliation_matrix`(统一四态判定，**单一 kill 决策点**)。

## 8. 未消费配置的最终裁决（任务单 §8）

**裁决：撤下 UI 暴露（方案 B）。**

| 参数 | 声称作用 | 实际情况 | 处置 |
|------|---------|---------|------|
| `BUY_DIP_PCT` | 相对 VWAP 折价买入阈值 | VWAP 折价行为**存在**，但由 `strategy_buy.py:55-56` 里**硬编码的 `0.02`** 实现，从未读过该配置 | **移除**（FieldSpec + Settings 字段 + TRACKED_PARAMS） |
| `SELL_PROFIT_PCT` | 止盈比例 | 已被更通用的 `sell_take_profit_ladder`（分批止盈阶梯）取代 | **移除**（同上） |

**为什么不选方案 A（接线）**：`buy_dip_pct` 默认 `0.005` 与硬编码的 `0.02` **相差 4 倍** ——
若接上去，会**实质改变入场评分**。当前回测与运行都是基于 `0.02` 的，
在小资金验证前做这种静默的行为变更不可接受。这本身就是"它从未被接线"的证据。

**防回归**：`tests/unit/test_v126_dead_config_switches.py` 的 `test_field_stays_removed`
已把两者列入"不得复活"。该守卫会在**任何可编辑字段无业务读取**时变红 ——
即"以后不得再往页面塞不生效的开关"。

## 9. 本轮未覆盖 / 已知脆弱点

| 项 | 状态 | 说明 |
|----|------|------|
| 闸门位于 `execute()` **之外** | ⚠️ 结构性脆弱点 | 当前无绕过调用点，但出口本身不强制闸门。属架构改动，未实施 |
| Pi 真实部署验证 | **NOT_EXECUTED** | 无 SSH 通道；见 progress.md 的诚实标注 |
| 断电/重启恢复实测 | **NOT_EXECUTED** | 需 Pi 环境 |
| 网络异常注入实测 | 部分（有单测） | 真实断网未做 |
| `/ops` 重排 | 见 progress.md | |
