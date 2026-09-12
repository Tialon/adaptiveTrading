# 测试网无人值守运维手册(Testnet Operational Runbook)

> V11.6 P1-6 交付(soak 停机/证据元数据在 V11.7 P0-2/P0-4 增强)。这是「真实币安测试网 +
> 财务真相闭环 + 无人值守观察」阶段的**运维手册**, 覆盖 7~24h soak 如何跑、如何验证、如何收集证据。
> 通用启动/排障/API 见 [runbook.md](runbook.md); 本文只写测试网验证阶段特有的操作。

## 1. 目标与就绪等级

| 项 | 值 |
|----|----|
| 目标 | 从「代码就绪」迈向「测试网已验证 + 财务真相闭环 + 可无人值守观察」 |
| 就绪等级 | 当前 **L2(运行时可靠)**; 完成 7~24h 稳定无人值守 → 评估 **L3(测试网无人值守交易)** |
| 标的 | 单币 **SOLUSDT**, 现货(非合约) |
| 资金 | 测试网约 2 万 RMB 等价(测试网币, 无真实价值) |
| 冻结 | 不新增策略/币种/合约/HFT/LLM 自动下单 |

## 2. 前置条件

1. **测试网 API key**: 币安测试网 `BINANCE_TESTNET_API_KEY` / `BINANCE_TESTNET_API_SECRET`
   (在 [testnet.binance.vision](https://testnet.binance.vision) 申请, 与主网 key 完全隔离)。
2. **测试网余额**: 账户需有足够 USDT/SOL(低于最小下单额时, 真实成交测试会跳过, 不视为代码缺陷)。
3. **网络可达**: 能访问 `testnet.binance.vision`(REST)与 `wss://stream.testnet.binance.vision/ws`(WS)。

> 密钥安全: `.env` 已 gitignore, **绝不提交或发布**; 本文与所有 artifact 均不记录真实 key/secret。

## 3. 环境配置(soak 用 .env)

测试网真实下单(非纸面)需要 `PAPER_TRADING=false` + `BINANCE_TESTNET=true` + 测试网 key。
`validate()` 会 fail-fast 校验这三者齐全, `mainnet_blocked_reason()` 保证 `BINANCE_TESTNET=true`
时绝无主网拦截。

```ini
SYMBOLS=SOLUSDT
PAPER_TRADING=false                 # 关键: 真实下单(测试网)
BINANCE_TESTNET=true                # 三重主网守卫之一(非 true 会被 validate/守卫拦截)
BINANCE_TESTNET_API_KEY=...         # 测试网 key(勿混用主网)
BINANCE_TESTNET_API_SECRET=...
DATABASE_URL=sqlite+aiosqlite:///./adaptive.db
REDIS_ENABLED=false                 # soak 期零依赖, 事件总线降级为内存
API_HOST=127.0.0.1                  # 仅本机(若需局域网看板再改 0.0.0.0 + WEB_ADMIN_TOKEN)
WEB_ADMIN_TOKEN=                    # 留空 = 写接口锁定(fail-closed); 需手动操作时再设
AI_ENABLED=false                    # soak 期关闭 AI(仅参数建议, 非下单路径; 减少噪声)
RECONCILE_INTERVAL_SECONDS=300      # 持仓对账周期
STARTUP_RECONCILE_ENABLED=true      # 启动对账(实盘差异未解决 -> 急停)
EQUITY_RECONCILE_TOLERANCE_PCT=0.02
```

## 4. 启动与健康自检

```powershell
.venv\Scripts\python run.py
```

启动后按顺序自检(全绿才视为「健康启动」):

1. **日志无 `拒绝主网启动` / `配置校验失败`**: 这两条是 fail-fast, 出现即未启动。
2. **面板可读**: `http://localhost:8800/api/metrics` 返回 `state=TRADING`(或 `SAFE` 启动早期)。
3. **交易许可**: `health.can_buy` 为 true 时才可能开新仓; 为 false 时看 `buy_block_reason`
   定位(急停/对账/行情/熔断/关键任务/停机任一维)。
4. **对账收敛**: 启动对账通过(未通过会急停冻结, 见 §7)。

单条健康自检命令:

```powershell
# health 快照(交易许可 + 各维健康)
curl http://localhost:8800/api/metrics
# 总览(权益/风控/regime/状态机)
curl http://localhost:8800/api/system
```

一键 soak(启动 run.py + 周期采样 + 证据落盘 + 到点摘要):

```powershell
# 测试网真实下单 soak 7 小时(证据写入 logs/soak/<run_id>/ 目录, V11.7 P0-4)
python -m at01_common.soak --hours 7
# 纸面模式 / 只采样已运行实例
python -m at01_common.soak --hours 24 --paper
python -m at01_common.soak --hours 7 --no-launch
```

## 5. 无人值守监控清单

soak 期间(7~24h)应能用一个词回答「现在能不能交易、为什么不能」——看 `/api/metrics` 的 `state`:

| state | 含义 | 是否可交易 |
|-------|------|-----------|
| TRADING | 交易中 | 正常 |
| SAFE | 就绪/启动早期 | 尚未开始, 观察 |
| DEGRADED | 降级 | 禁开仓, 可安全离场 |
| REDUCE_ONLY | 仅减仓 | 禁开新仓, 保留卖出 |
| PAUSED | 暂停 | 禁开新仓 |
| RECOVERY | 恢复核验 | 禁交易, 待确认 |
| KILLED | 冻结 | 禁交易, 需人工恢复 |

关键指标(2s WS 或轮询):

- `health.can_buy` / `health.can_sell` + `buy_block_reason` / `sell_block_reason`: 交易许可真相。
- `health.tasks.failure_count`: 后台任务崩溃计数(关键任务崩溃 → `critical_tasks_healthy=false` → 禁 BUY)。
- `health.reconcile.reconciled`: 对账是否通过。
- `health.market.ws_silence_seconds`: 行情静默(≥30s 告警)。
- `health.kill_switch.armed`: 是否急停冻结。

## 6. 财务真相闭环验证

实盘(测试网)财务真相 = **交易所真相**(`ExchangeTruthReconciler` + `ReconciliationMatrix` 单一权威),
`AccountLedger` 仅纸面模式落库(实盘 cash_before=None, 现金维度由权益对账兜底)—— 这是 V11.6 P0-3
钉死的诚实边界, 不要误以为实盘账本没写就是 bug。

验证点:

1. **权益对账**: 本地权益 vs 交易所权益(容差 `EQUITY_RECONCILE_TOLERANCE_PCT`), 漂移超阈值 →
   急停冻结。
2. **交易所真相成交**: `get_my_trades_all` 去重 + 完整性检测(重复/跳号/翻页耗尽)。
3. **跨源漂移分级**: `fund_circuit_breaker` 按 Equity/Position/Cash 三向 0.1%/0.2%/0.5% 分级
   (REDUCE_ONLY → PAUSE → KILL); 跨源资金级差异(equity_drift/orphan_trade/fill_truth_*)
   **单源即 KILLED**。
4. **本地 DB 内部一致性**: 交叉对账(Order/Fill/Ledger/Lot)单一对账器只 RECOVERY_REQUIRED
   (自愈不 kill), 需 ≥2 独立对账器同周期佐证才升级 KILLED。

## 7. 告警与处置

| 现象 | 处置 |
|------|------|
| `state=KILLED`(急停冻结) | 查日志定位差异 → 人工确认收敛后 `POST /api/emergency/recover`(重启不自动复位) |
| `state=RECOVERY`(恢复核验) | 待对账确认一致后 `confirm_recovered`(禁止裸 reset) |
| `state=PAUSED`(暂停/熔断) | 冷却自动恢复或 `POST /api/breaker/reset` |
| `state=REDUCE_ONLY`(仅减仓) | 风控去险; 保留卖出, 禁开新仓 |
| `can_buy=false` + `关键后台任务` | 有 critical 任务崩溃 → 已进 SAFE_MODE; 查 `health.tasks` 定位后人工处置(无自动重启) |
| `ws_silence_seconds` 持续高 | WS 断线, 自动重连; 查网络/代理 |
| 行情异常(价格 spike) | 风控异常暂停 60s, 自动恢复 |

写操作鉴权: 所有 POST 需 `X-Admin-Token` 头 = `WEB_ADMIN_TOKEN`(soak 默认留空 = 写接口锁定;
需手动操作时先设非空令牌再请求)。示例:

```powershell
curl -X POST http://localhost:8800/api/emergency/recover -H "X-Admin-Token: <token>"
```

## 8. 证据收集(供 P1-8)

soak 结束/过程中收集以下证据(注意脱敏, 不记录 key/secret):

| 证据 | 位置 |
|------|------|
| soak 元数据/摘要 | `logs/soak/<run_id>/metadata.json` + `summary.json`(V11.7 P0-4: run_id/git_sha/duration/final_state/acceptance_result) |
| soak 逐行证据 | `logs/soak/<run_id>/evidence.jsonl`(逐行 health 快照) |
| soak 运行日志 | `logs/soak/<run_id>/runtime.log` |
| 结构化日志 | `logs/adaptive.log`(JSON, 含启动/对账/告警/状态迁移) |
| 数据库快照 | SQLite `adaptive.db`(28 张表 + schema_version) |
| 每日复盘 | `reports/YYYY-MM-DD.md`(DAILY_REPORT_ENABLED=true 自动产出) |
| 对账/账务证据 | `orders` / `order_fills` / `positions` / `position_lots` / `sell_allocations` |
| health 快照 | `/api/metrics` 周期采样(记录 state/can_buy/对账状态随时间的演化) |

## 9. 安全停机与恢复

- **优雅停机**: `Ctrl+C` 或 `POST /api/shutdown`(需令牌)。停机前先确认无在途信号。
- **急停持久化**: `kill_switch_state` 单行落库; 重启后仍保持冻结(不自动复位), 需人工
  `recover` 解除 —— 这是设计行为, 防止重启掩盖未解决的资金差异。
- **重启自愈**: 重启自动 `create_all` + `upgrade_schema`(前向迁移)+ 恢复急停/持仓状态。

## 10. 诚实边界

- 本阶段仍是**测试网**, 非主网; 不新增策略/币种/合约/HFT/LLM 自动下单。
- 实盘(测试网)路径 `AccountLedger` 不落库(cash_before=None), 现金真相由权益对账兜底。
- 后台任务**无自动重启**(崩溃 → SAFE_MODE + 禁 BUY, 需人工处置)。
- 无人值守 ≠ 无人看管: 仍建议周期查看 `state` 与 `kill_switch.armed`, 冻结/崩溃需人工介入。
