# adaptiveTrading V11.0 — Production Readiness

> 定位: 不是增加赚钱策略, 而是证明——即使交易所 / 网络 / 进程 / 数据库 / Redis / WebSocket /
> 订单状态发生异常, 系统仍不会错误地修改资金账本。
> 原则: 质量 > 数量; 不再新增 RSI/MACD/Bollinger/AI/ML/Transformer/RL 等策略。
> 冻结不变: Binance 单所 / SOLUSDT 单币 / 双仓 / 低频; AI 只分析优化、不直接下单。

## 评审基线(基于 commit b2dd5e2)

- 综合: 工程完整度约 **84/100**; 实盘准备度约 **70/100**。
- 结论: 核心架构已成型, 瓶颈从「有没有功能」转向「有没有证据证明异常下不出现资金级错误」。
- 方向: 停止堆功能, 进入「证明正确性」阶段。

## P0 — 执行可靠性(下一轮最优先)

| # | 任务 | 现状缺口 | 目标 |
|---|------|---------|------|
| P0-1 | Exchange Truth V2 | myTrades 仅 `limit=100`, 依赖「15 分钟 ≤100 笔成交」假设; 恢复链路 `fee=0` 缺手续费 | 分页 fromId / startTime / endTime 覆盖完整成交; 恢复用 commission / commissionAsset 重建 Fill |
| P0-2 | Ledger Reconstruction | PositionLot / SellAllocation 丢失后无法从交易所真相重建 | Exchange → Orders → Trades → Buy Lots → Sell Allocation → Position → Portfolio → Equity 全链重建 |
| P0-3 | SELL Recovery | RECOVERY_REQUIRED SELL 仅人工冻结(FIFO 分配信息丢失) | 消除人工处理, SELL 自动自愈 |
| P0-4 | Reconciliation Engine V2 | 各对账器分散, 无统一矩阵 | Order / Fill / Position / Lot / Cash / Equity / Ledger 的 Truth Reconciliation Matrix |

## P1 — 回测 / 风控 / 可观测性

| # | 任务 | 说明 |
|---|------|------|
| P1-1 | Backtest V2 | 时间 7/30/90/180/365 天 × 市场态(BULL/NORMAL/SIDEWAY/VOLATILE/BEAR/PANIC)× 参数(baseline/±5%/±10%)× 执行(0/5/10/20/30bps)矩阵; 输出 Robustness 而非单一 return |
| P1-2 | Optimizer V2 | 网格搜索 → Walk-Forward → Robustness → 风险调整排序; 防「历史最优 ≠ 未来最优」过拟合 |
| P1-3 | System Lifecycle | 顶层状态机 INIT/WARMING_UP/SYNCING/SELF_CHECK/READY/TRADING/DEGRADED/RECOVERY/SAFE_MODE/STOPPED; 与 RiskState + 连接状态 + 对账状态共同决定 CanTrade |
| P1-4 | 生产可观测性 | 执行延迟 / 对账漂移 / 恢复次数 / 订单失败率 / 数据缺口 / 策略归因 + 告警 |
| P1-5 | 资金级 Circuit Breaker | Equity / Position / Cash 三向漂移分级处置(0.1%/0.2%/0.5%); Position / Cash 漂移 → REDUCE_ONLY |

## P2

| # | 任务 |
|---|------|
| P2-1 | 执行 Dashboard(订单/REST/WS/Fill 延迟、拒绝率、重试、UNKNOWN、恢复订单) |
| P2-2 | 对账 Dashboard(Position/Cash/Order/Fill/Ledger 漂移 + 最近成功对账时间) |
| P2-3 | 策略 Dashboard(Signal→Order→Fill→PnL→Strategy 归因) |
| P2-4 | AI 权限架构级隔离(at50_strategy 禁止 import execution) |

## 验证目标(启动时补)

- [ ] 全量测试回归通过(当前 497/497)
- [ ] 画出 Order → Fill → Lot → Position → Ledger → Equity 完整资金守恒链
- [ ] 找出所有「重复下单 / 重复记账 / 漏记账 / 错误急停 / 错误恢复 / 资金漂移」路径

## 启动前置(评审建议的下一轮深度审查)

1. 审查 `at50_execution` 全部执行代码
2. 审查 `at60_risk` 全部风控代码
3. 审查 `at01_common/models.py` 全部账务模型
4. 审查 `run.py` 主循环
5. 对照 CrossReconciler / PositionReconciler / ExchangeTruthReconciler / OrderRecovery
6. 对照 `init.sql` 与全部 `tests/unit + tests/integration`
