# 生产就绪检查清单(V11.4)

> 面向「无人值守长期运行」的就绪核对清单。每条对应一处已落实的加固点;
> 打勾项均有代码/测试锚点, 非口头承诺。相关细节见 [runbook.md](runbook.md)、
> [architecture.md](architecture.md)、[metrics-persistence.md](metrics-persistence.md)。

## 就绪等级(2026-09-08)

| 等级 | 含义 | 状态 |
|------|------|------|
| L1 | 离线正确性(单元/集成/财务不变量测试) | ✅ 达成(V11.0~V11.3) |
| L2 | 运行时验证(长跑 soak + 故障注入 + 恢复审计 + 测试网只读冒烟) | ✅ 达成(V11.4) |
| L3 | 测试网无人值守实盘(真实下单长跑) | ⬜ 待 ops 部署验证(非代码任务) |
| L4 | 主网无人值守实盘 | ⬜ 未达(§8 已知限制 + 需先过 L3) |

**当前: L2(运行时验证就绪)。** V11.4 从「测试证明正确」迈向「长跑证明正确」:
P0 长跑 soak(24 周期 + 6 类故障注入, 逐周期 5 条财务不变量)、异常绝不继续 BUY、
恢复状态机穷举审计、重启/崩溃 soak、真实币安测试网只读冒烟、schema 全列清单、
run.py 运行时监督审计(修复 6 处潜在 NameError 死路径)全部落地; CI 加 ruff 正确性基线 +
coverage 阈值 75%。L3(测试网实盘)需在部署环境真实跑 `run.py` 观察, 属运维动作, 不在本次代码交付内。

## 0. 冻结产品定义(不可变更)

- [x] Binance 单所 / SOLUSDT 单币 / Spot 现货(非合约)/ 双仓 / 低频摆动
- [x] AI 只分析与参数优化, **不直接下单**(优化结果仅提案, 见 §6)
- [x] 不新增交易策略 / 不扩展币种 / 不增加 Futures / 不增加高频 / 不增加 LLM 自动下单

## 1. 安全默认(default-safe)

- [x] `PAPER_TRADING=true` 出厂默认; `BINANCE_TESTNET=true` 出厂默认
- [x] 主网守卫: `BINANCE_TESTNET=false` 且未显式 `LIVE_TRADING_CONFIRM=true` → 启动拦截
  (`Settings.mainnet_blocked_reason`, 测试 `test_v140_mainnet_guard.py`)
- [x] 配置 fail-fast: 实盘缺 key / 空标的 / 三桶比例和≠1 / 非法阈值 → 拒绝启动
  (`Settings.validate`, 测试 `test_v128_config_audit.py`)
- [x] 冻结单币: 非 SOLUSDT 由 `validate()` fail-fast(不静默多币种)

## 2. 启动前检查(pre-flight)

- [x] `.env` 从 `.env.example` 复制(默认 SOLUSDT / 纸面 / 测试网); 密钥仅存本地, 不入库不入 git
- [x] 首次运行 `create_all` 自动建 25 张表(SQLite 默认零依赖; 生产切 MySQL)
- [x] 启动对账(仅实盘): 崩溃窗口恢复 + 未解决歧义 → 急停冻结(不静默改账)
- [x] 急停状态持久化(单行 id=1), 重启**不自动复位**

## 3. 运行期可靠性

- [x] 对账矩阵四态处置(PASS/DEGRADED/RECOVERY_REQUIRED/KILLED)+「单一对账器不得 kill」
- [x] 资金级熔断三向漂移分级(Equity 0.1%/0.2%/0.5%; Position/Cash 首选 REDUCE_ONLY)
- [x] 统一交易闸门六维(生命周期 + 风险态 + 行情 + 交易所 + 对账 + 资金熔断)
- [x] 停机前 `flush_events` 等待在途风险事件落库(防审计事件丢失)
- [x] 异常保护告警降噪(仅状态切换时告警, 不刷屏)
- [x] 长跑 soak: 24 周期(简单+多 lot 交替)+ 6 类故障注入(WS 断开/REST 超时/重复成交/部分成交/部分成交后撤单/未知订单), 逐周期断言 5 条财务不变量(`test_24h_soak.py`)
- [x] 异常状态绝不继续 BUY: `RiskManager.can_buy` 全路径钉死(`test_v142_abnormal_never_buy.py`)
- [x] 恢复状态机穷举审计: NORMAL/REDUCE_ONLY/PAUSED/KILLED/RECOVERY_CHECK 全转移(`test_v143_recovery_state_machine_audit.py`)
- [x] 重启/崩溃 soak: 急停持久化(单行 id=1)+ 重启不自动复位 + 状态收敛(跨进程)
- [x] run.py 运行时监督审计: 修复 6 处局部导入 NameError 死路径(熔断决策/对账 KILL/双仓/core ADD·REDUCE/告警)

## 4. 数据完整性

- [x] 25 张表(SCHEMA_VERSION=V11.2), schema 全列清单钉死(259 列, `test_v129_schema_audit.py` 全列 inventory 捕获 create_all 静默列漂移)
- [x] 手续费会计: 跨 live/重建路径逐位一致, 费用恰好一次, base 资产(SOL)折算
- [x] 账本重建(交易所真相重建账务)+ 现金/权益守恒检查
- [x] Order/Fill/Ledger/Lot 四维交叉对账(漂移 → 急停)

## 5. 可观测性

- [x] `MetricsStore`(计数器/仪表/延迟样本/策略归因); 延迟样本有界(10k)
- [x] 阈值告警: 订单失败率 5% / 对账漂移 2% / 数据缺口 300s / 延迟 P95 5000ms / 连续恢复 5
- [x] `recovery_streak`/`recoveries` 恢复计数接线(P0-10 修复死指标)
- [x] 指标持久化评估: 保持内存(重启归零语义正确), 复盘数据已由 DB 源表覆盖

## 6. 优化与变更(只提案, 不自动生效)

- [x] Optimizer 落 `strategy_versions` 时 `active=False`; `activate` 为独立显式方法,
  代码层无任何 `.activate(` 自动调用(测试 `test_v141_optimizer_proposal_only.py`)
- [x] 参数生效需人工: 审阅提案 → `activate(version)` 标记 → 重启/改配置应用

## 7. 部署

- [x] CI(推 main + PR, Python 3.13): ruff 正确性基线(E9+F, 忽略 F401)+ coverage 阈值 75%(实测 78.9%)
- [x] 测试全本地(SQLite 内存, 无外部依赖); 真实测试网冒烟显式 `-m "not testnet"` 排除于 CI(双保险: fixture 未设 `RUN_TESTNET_SMOKE` 亦 skip)
- [x] 本地默认 SQLite + Redis 关闭零依赖; 生产(Pi)MySQL 8 + Redis(1panel)
- [x] 1032 测试全绿(含故障注入 / 长跑 soak / 重启 / 财务不变量)+ 4 真实测试网冒烟(opt-in)

## 8. 已知限制(诚实披露)

- [ ] 指标为内存态, 进程重启归零(设计如此; 跨重启趋势需未来加 `metrics_snapshot`)
- [ ] `activate()` 仅置 DB 标记, 不自动改写运行参数(需人工重启/改 .env)
- [ ] 存量库结构变更需手动 `ALTER`(见 runbook「数据库迁移」), 无 Alembic
- [ ] 启动对账为一次性(非周期); 运行期漂移由周期 `_reconcile_loop` 兜底
- [ ] 实盘 cash_after 为近似值(由权益对账兜底, 非逐笔现金精确核对)
- [ ] 测试网只读冒烟(连通/规则/行情/鉴权)尚未含真实下单——L3 需在部署环境真实跑 `run.py` 观察
