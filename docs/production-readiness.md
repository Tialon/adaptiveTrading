# 生产就绪检查清单(V11.8)

> 面向「无人值守长期运行」的就绪核对清单。每条对应一处已落实的加固点;
> 打勾项均有代码/测试锚点, 非口头承诺。相关细节见 [runbook.md](runbook.md)、
> [architecture.md](architecture.md)、[metrics-persistence.md](metrics-persistence.md)。

## 就绪等级(2026-09-08)

| 等级 | 含义 | 状态 |
|------|------|------|
| L1 | 离线正确性(单元/集成/财务不变量测试) | ✅ 达成(V11.0~V11.3) |
| L2 | 运行时验证(长跑 soak + 故障注入 + 恢复审计 + 测试网冒烟 + **运维加固**) | ✅ 达成(V11.4→V11.5) |
| L3 | 测试网无人值守实盘(真实下单闭环长跑) | ⬜ 待 7h/24h soak 在部署环境跑通(见状态模型) |
| L4 | 主网无人值守实盘 | ⬜ 未达(§8 已知限制 + 需先过 L3) |

**等级语义(V11.5 重定义)**: L2「运行时验证」从「长跑证明正确」扩展为「运行证明可靠」——
在 V11.4 的长跑/故障注入/恢复审计/只读冒烟之上, V11.5 补齐**运维加固**五件套:
运行时任务监督(RuntimeSupervisor)、统一运行时健康快照(Runtime Health)、
故障注入(Fault Injection)、类型/静态审计(Type/Static Audit)、
依赖/供应链(Dependency/Supply Chain), 使「能不能交易、为什么不能」可被一个快照一个词回答。

**当前: L2(运行时验证就绪)。** V11.8 代码与测试全部落地(V13 时点 2500 非 testnet 测试全绿;
coverage 80% ≥ 75%, ruff 全绿)。**真实测试网订单生命周期已实际执行并 PASSED**
(`RUN_TESTNET_TRADING=1` 跑通 test_v152: LIMIT no-fill→cancel + MARKET BUY/SELL 0.072 SOL 闭环),
**Docker 生产运行时已落地 + amd64 冒烟通过**(镜像构建 + 容器 `/api/health` 200 + graceful shutdown);
但按 V11.7 P1-9 规则「只有一次 BUY/SELL → 仍 L2」, 且 7h/24h 无人值守 soak **未执行(NOT_EXECUTED)**、
ARM64(Pi)构建未执行, 故诚实判定**仍为 L2、不虚报 L3**(见 [testnet-operation.md](testnet-operation.md))。

## 状态模型(V11.7 P0-1)

「代码已实现」与「真实环境已执行」严格区分, 统一七态: `IMPLEMENTED` / `READY_TO_RUN` /
`EXECUTED` / `PASSED` / `FAILED` / `BLOCKED` / `NOT_EXECUTED`。八个跟踪项:

| 项 | 状态 |
|----|------|
| Testnet connectivity | EXECUTED → **PASSED** |
| Testnet order lifecycle(no-fill) | EXECUTED → **PASSED** |
| real BUY | EXECUTED → **PASSED** |
| real SELL | EXECUTED → **PASSED** |
| financial reconciliation(单笔) | EXECUTED → **PASSED** |
| 7h soak | READY_TO_RUN → **NOT_EXECUTED** |
| 24h soak | READY_TO_RUN → **NOT_EXECUTED** |
| L3 readiness | **NOT_EXECUTED(仍 L2)** |

## 0. 冻结产品定义(不可变更)

- [x] Binance 单所 / SOLUSDT 单币 / Spot 现货(非合约)/ 双仓 / 低频摆动
- [x] AI 只分析与参数优化, **不直接下单**(优化结果仅提案, 见 §6)
- [x] 不新增交易策略 / 不扩展币种 / 不增加 Futures / 不增加高频 / 不增加 LLM 自动下单

## 1. 安全默认(default-safe)

- [x] `PAPER_TRADING=true` 出厂默认; `BINANCE_TESTNET=true` 出厂默认
- [x] 主网守卫: `BINANCE_TESTNET=false` 且未显式 `LIVE_TRADING_CONFIRM=true` → 启动拦截
  (`Settings.mainnet_blocked_reason`, 测试 `test_v140_mainnet_guard.py`)
- [x] **主网就绪自检(V11.8 P0-4)**: `BINANCE_TESTNET=false` 时启动前强制 `MAINNET_READINESS_CHECK`
  九项判定(连主网/非纸面/显式确认/API 权限确认/单币/配置审计/非急停/git_sha+主网端点),
  任一不满足 → BLOCKED 拒绝启动; `MAINNET_API_SCOPE_CONFIRM` 默认 false → 主网默认必被拦
  (测试 `test_v175_mainnet_readiness.py`, 人工复审见 [mainnet-readiness.md](mainnet-readiness.md))
- [x] 配置 fail-fast: 实盘缺 key / 空标的 / 三桶比例和≠1 / 非法阈值 → 拒绝启动
  (`Settings.validate`, 测试 `test_v128_config_audit.py`)
- [x] 冻结单币: 非 SOLUSDT 由 `validate()` fail-fast(不静默多币种)
- [x] **Web 面板安全(V11.5 P0-1)**: `API_HOST=127.0.0.1` 出厂默认(仅本机回环);
  写接口统一 `X-Admin-Token` 头鉴权(`WEB_ADMIN_TOKEN` 空 → 面板锁定; 错 → 401);
  `API_HOST=0.0.0.0`(局域网/公网)需非空 `WEB_ADMIN_TOKEN`, 否则 `validate()` fail-fast
  (测试 `test_v150_web_security.py`)

## 2. 启动前检查(pre-flight)

- [x] `.env` 从 `.env.example` 复制(默认 SOLUSDT / 纸面 / 测试网); 密钥仅存本地, 不入库不入 git
- [x] 首次运行 `create_all` 自动建 29 张表(SQLite 默认零依赖; 生产切 MySQL)
- [x] 启动对账(仅实盘): 崩溃窗口恢复 + 未解决歧义 → 急停冻结(不静默改账)
- [x] 急停状态持久化(单行 id=1), 重启**不自动复位**
- [x] **运行时任务监督(V11.5 P0-2)**: `RuntimeSupervisor` 统一 spawn 命名后台任务、
  跟踪运行/完成/取消/异常, critical 任务异常退出 → 同步回调进入安全态 + 急停,
  graceful shutdown 幂等取消回收(测试 `test_v151_runtime_supervisor*.py`)

## 3. 运行期可靠性

- [x] 对账矩阵四态处置(PASS/DEGRADED/RECOVERY_REQUIRED/KILLED)+「单一对账器不得 kill」
- [x] 资金级熔断三向漂移分级(Equity 0.1%/0.2%/0.5%; Position/Cash 首选 REDUCE_ONLY)
- [x] 统一交易闸门(生命周期 + 风险态 + 行情 + 交易所 + 对账 + 资金熔断 **六维**,
  V11.6 BUY 安全契约追加**停机窗口 + 关键任务健康**两维 → 开仓实为 6+2 共八项)
- [x] 停机前 `flush_events` 等待在途风险事件落库(防审计事件丢失)
- [x] 异常保护告警降噪(仅状态切换时告警, 不刷屏)
- [x] 长跑 soak: 24 周期 + 6 类故障注入, 逐周期断言 5 条财务不变量(`test_24h_soak.py`)
- [x] 异常状态绝不继续 BUY: `RiskManager.can_buy` 全路径钉死(`test_v142_abnormal_never_buy.py`)
- [x] 恢复状态机穷举审计: 全转移矩阵(`test_v143_recovery_state_machine_audit.py`)
- [x] 重启/崩溃 soak: 急停持久化 + 重启不自动复位 + 状态收敛(跨进程)
- [x] run.py 运行时监督审计: 修复 6 处局部导入 NameError 死路径
- [x] **统一运行时健康快照(V11.5 P1-1)**: `build_runtime_health` + 七态分类器
  (KILLED/RECOVERY/PAUSED/REDUCE_ONLY/DEGRADED/TRADING/SAFE), `/api/metrics` 一个词回答
  「现在能不能交易、为什么不能」(测试 `test_v154_runtime_health.py`)
- [x] **运行时故障注入(V11.5 P1-2)**: critical 任务崩溃 → SAFE_MODE + 禁 BUY;
  停机中在途信号丢弃不触达执行引擎; WS 断连禁开仓→重连收敛(测试 `test_v155_fault_injection.py`)
- [x] **BUY 安全契约(V11.6 P0-2)**: 停机/关键任务两维收口到统一交易闸门, 停机窗口在途信号
  不得偷建仓(`test_v160_buy_safety_contract.py`)
- [x] **运行时健康契约(V11.6 P1-2)**: `health.can_buy/can_sell` 直接取自统一闸门,
  单一权威无分叉(`test_v162_runtime_health_contract.py`)
- [x] **RuntimeSupervisor 第二轮审计(V11.6 P1-3)**: 回调异常 / 取消 / 无重启契约钉死
  (`test_v163_runtime_supervisor_audit2.py`)

## 4. 数据完整性

- [x] 29 张表(SCHEMA_VERSION=V13.0), schema 全列清单钉死, `test_v129_schema_audit.py`)
- [x] 手续费会计: 跨 live/重建路径逐位一致, 费用恰好一次, base 资产(SOL)折算
- [x] 账本重建(交易所真相重建账务)+ 现金/权益守恒检查
- [x] Order/Fill/Ledger/Lot 四维交叉对账(漂移 → 急停)
- [x] **实盘财务真相闭环(V11.6 P0-3/P1-1)**: AccountLedger 仅纸面落库(paper-only),
  实盘锚定交易所真相对账链(Order/OrderFill/Position/PositionLot/SellAllocation),
  `LIVE_ACCOUNT_LEDGER_MODE = EXCHANGE_TRUTH_RECONCILIATION`(`test_v161_live_financial_truth.py`)
- [x] **数据库迁移工程(V11.5 P0-4 → V11.6 P1-4)**: V11.5 以 schema 稳定性锚点 +
  `test_v153_schema_check.py` 兜底(全列 inventory 捕获 create_all 静默列漂移);
  V11.6 P1-4 补上**最小前向迁移框架**(`at01_common/migrations.py::upgrade_schema` +
  `migrations/*.sql` + `schema_version` 簿记表), 启动 `init_db` 自动应用未落库迁移
  (`test_v164_db_migration.py`, 见 [database-migration.md](database-migration.md))

## 5. 可观测性

- [x] `MetricsStore`(计数器/仪表/延迟样本/策略归因); 延迟样本有界(10k)
- [x] 阈值告警: 订单失败率 5% / 对账漂移 2% / 数据缺口 300s / 延迟 P95 5000ms / 连续恢复 5
- [x] `recovery_streak`/`recoveries` 恢复计数接线(修复死指标)
- [x] 指标持久化评估: 保持内存(重启归零语义正确), 复盘数据已由 DB 源表覆盖
- [x] **健康快照接入 `/api/metrics`(V11.5 P1-1)**: 生命周期/风险态/急停/熔断/对账/市场/
  交易所/后台任务/交易/最近事件/最近错误, 聚合为单一 `health` 字段

## 6. 优化与变更(只提案, 不自动生效)

- [x] Optimizer 落 `strategy_versions` 时 `active=False`; `activate` 为独立显式方法,
  代码层无任何 `.activate(` 自动调用(测试 `test_v141_optimizer_proposal_only.py`)
- [x] 参数生效需人工: 审阅提案 → `activate(version)` 标记 → 重启/改配置应用

## 7. 部署

- [x] CI(推 main + PR, Python 3.13): ruff 正确性基线(E9+F)+ coverage 阈值 75%
- [x] **ruff F401 全局忽略移除(V11.5 P1-3)**: 批量清理 28 处真实未使用导入, E9+F 全绿
- [x] **类型检查(V11.5 P1-3)**: mypy 接入(适度, 核心模块 only, 非 CI 门禁)——修复 6 处真实
  缺陷(漏 await 风控事件/None 解引用/错误返回注解/动态注入未声明); 剩余 24 处为动态注入
  可选依赖的 union-attr/arg-type 噪声, 按「不强行 strict mypy」保留
- [x] **依赖/供应链(V11.5 P1-4)**: pyproject 显式声明 `cryptography`(MySQL caching_sha2 认证)
  与 `websockets`(uvicorn WS)运行时必需依赖; `uv.lock` 重新解析同步(52 包, 含 dev 依赖组);
  `pip-audit` 应用运行时依赖 0 已知漏洞(仅 pip 25.3 构建工具有 6 CVE, 非运行时)
- [x] 测试全本地(SQLite 内存, 无外部依赖); 真实测试网冒烟/下单显式 `-m "not testnet"` 排除于 CI
- [x] 本地默认 SQLite + Redis 关闭零依赖; **生产(Pi)SQLite 单机 + WAL/busy_timeout/foreign_keys 加固**
  (V11.8 P0-3, 见 [database-migration.md](database-migration.md))
- [x] **Docker 生产运行时(V11.8 P0-1/P0-2)**: 多阶段 Dockerfile(uv frozen + tini PID1 + 非 root)+
  docker-compose(restart: unless-stopped + SQLite 持久化卷 + HEALTHCHECK), 见 [docker-deployment.md](docker-deployment.md)
- [x] **树莓派 arm64 部署路径(V11.8)**: `python:3.13-slim` 多架构 + 无人值守自检清单,
  见 [raspberry-pi-deployment.md](raspberry-pi-deployment.md)
- [x] **CI Docker build + smoke(V11.8 P0-6)**: `ci.yml` 加 docker-smoke job(镜像构建 + 容器 `/api/health` 200)
- [x] 1499 非 testnet 测试全绿 + 6 testnet 测试(opt-in, CI 排除)+ coverage ≥ 75%

## 8. 已知限制(诚实披露)

- [ ] 指标为内存态, 进程重启归零(设计如此; 跨重启趋势需未来加 `metrics_snapshot`)
- [ ] `activate()` 仅置 DB 标记, 不自动改写运行参数(需人工重启/改 .env)
- [ ] 迁移框架为最小前向 DDL(无回滚 / 无 Alembic / 无自动生成), 结构变更仍需同步登记
  §2 三件事(见 database-migration.md); 差异检查 `schema_check` 为离线工具, 未接入启动路径
- [ ] 启动对账为一次性(非周期); 运行期漂移由周期 `_reconcile_loop` 兜底
- [ ] 实盘 cash_after 为近似值(由权益对账兜底, 非逐笔现金精确核对)
- [ ] **真实测试网 7h/24h 无人值守 soak 尚未执行**——单笔 BUY/SELL 闭环已实际跑通
  (`test_v152_testnet_order_lifecycle.py`, `RUN_TESTNET_TRADING=1` → 2 passed), 但 7h/24h 长跑
  需部署环境真实挂机, L3 仍未达(见 testnet-operation.md)
- [ ] mypy 为「建议性」检查, 24 处动态注入噪声未清理(union-attr/arg-type), 不纳入 CI 门禁
- [ ] `pip-audit` 报 pip 25.3 有 6 CVE(修复版本 ≥26.2)——pip 为构建工具非运行时依赖, 低危,
  升级 venv 内 pip 即可消除(不属仓库交付物)
- [x] Web 面板默认 `API_HOST=127.0.0.1`(V11.5 P0-1 加固); 写接口统一 `X-Admin-Token` 头鉴权
  (`WEB_ADMIN_TOKEN` 空则锁定, 错误则 401); 局域网/公网访问需显式 `API_HOST=0.0.0.0` + 非空
  `WEB_ADMIN_TOKEN`, 否则启动 `validate()` fail-fast 拦截
