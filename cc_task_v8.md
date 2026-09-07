# adaptiveTrading V8 — 生产加固与账务修复(13 项审查)

> 前提: V7 完成回测真实化; V8 聚焦无人值守实盘的**正确性 / 持久化 / 可恢复 / 安全**。
> 原则: 单一记账(ExecutionEngine 经 PortfolioEngine)、状态可持久化、重启可对账、主网有安全闸门。

## P0(账务与接线)

| # | 问题 | 修复 |
|---|------|------|
| 1 | 成交双重记账(`BucketPositionManager` 也 apply_buy/sell 总账) | `risk_buckets.py` 变纯拆分跟踪, 总账只由 ExecutionEngine 经 PortfolioEngine 记 |
| 2 | `PortfolioEngine` 未接线(创建晚于 ExecutionEngine) | `run.py` 重排创建顺序 |
| 3 | `on_order_canceled` 死代码(从未调用)、`on_order_submitted` 未调用 | `execute()` 调用 `on_order_submitted`; 异常/拒绝/零成交路径回退状态机 |
| 4 | `_confirm_fill` 未处理 `PARTIALLY_FILLED` | 保留部分成交, 撤单/终态不回吐已成交部分 |

## P1(持久化与对账)

| # | 问题 | 修复 |
|---|------|------|
| 5 | 交易状态机重启丢失 | `TradeStateRow` + `load_from_db` / `persist` / `reconcile_with_positions` |
| 6 | PaperBroker 现金重启丢失 | `PaperState` + `load_cash_from_db` / `save_cash_to_db` |
| 7 | 无交易所持仓对账 | `reconciliation.py::PositionReconciler`(对比 `get_account()` 与本地持仓) |
| 8 | 无行情数据校验 | `data_validator.py::MarketDataValidator`(K 线连续 + 价格跳变) |

## P2(安全与运维)

| # | 问题 | 修复 |
|---|------|------|
| 9 | AI 300s 调参过频且无审批 | `AI_INTERVAL_SECONDS` → 86400 + `ai_parameter_guard`(幅度阈值审批, 超阈值需人工) |
| 10 | Kill Switch 缺快速崩盘/余额不匹配 | `check_fast_crash`(15min −10%) + 对账 balance-mismatch |
| 11 | 日志无限增长 | `RotatingFileHandler`(10MB × 5) |
| 12 | `run.py` 冗余 `self.journal` | 移除 |
| 13 | 主网实盘无二次确认 | `live_trading_confirm` 守卫(非 testnet 且未确认 → 启动即拒绝) |

## 附: 回测卡死与测试依赖(顺带修复)

- **回测卡死**: 回测未建表 → `StrategyEngine` 落库(journal/信号)每次失败 → `logger.exception` 深 traceback 慢渲染(120 次/回测)。
  修复: `PortfolioBacktester.run()` 开头 `init_db()`(try/except 包裹)。
- **测试依赖**: 补装 `aiosqlite`(conftest 用 `sqlite+aiosqlite:///:memory:`, 环境此前缺失)。

## 交付物

- 新增模块: `at20_market/data_validator.py`、`at50_execution/reconciliation.py`、`at50_strategy/ai_parameter_guard.py`
- 新增表: `trade_state`、`paper_state`(全库 13 → 15 张)
- 新增配置: `live_trading_confirm`、`reconcile_interval_seconds`

## 验证

- **219/219 测试通过**(单元 199 + 集成 20)
- 全部改动/新增模块导入干净(含 `run.py`)
- 账务单一记账契约由 `test_v4` 固化(拆仓不再同步总账)
