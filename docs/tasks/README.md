# docs/tasks/ — 历史工单归档

> 这里是从仓库根目录收进来的 **18 份 `cc_task_*.md`**：每个版本开发前写的任务清单，
> 以及写完之后在同一份文件里追加的执行记录。

## ⚠️ 阅读前必读：包名已经改过

这些归档件写于 2026-09-12 之前，里面出现的 `atXX` 包名是**当时的旧名**，**不是现在代码里的名字**。
2026-09-12 做过一次按「阅读顺序 = 数据流顺序」的重编号（commit `bac9079`）：

| 归档件里的旧名 | 现在的名字 | 层 |
|---|---|---|
| `at01_common` | `at01_common`（未变） | L0 基础(横切) |
| `at20_market` | `at10_market` | L1 行情接入 |
| `at30_analytics` | `at20_analytics` | L2 分析 |
| `at50_strategy` | `at30_strategy` | L3 策略 |
| `at55_portfolio` | `at40_portfolio` | L4 组合 |
| `at60_risk` | `at50_risk` | L5 风控 |
| `at50_execution` | `at60_execution` | L6 执行 |
| `at40_journal` | `at70_journal` | L7 记录 |
| `at70_backtest` | `at80_backtest` | L8 研究-回测 |
| `at80_optimizer` | `at85_optimizer` | L8 研究-优化 |
| `at10_web` | `at90_web` | L9 展示(横切) |
| `at90_deploy` | **已删除**（死包） | — |

另外 `90_deploy/` 也已删除（内含 141 行过期的**手写表 DDL**，与 V11.0「表结构统一由 ORM 负责」的决策冲突）。

**归档件保留原样未改**——它们记录的是「当时发生了什么」，改写历史会让记录失真。

## 这些文件怎么用

| 你想知道 | 去看 |
|---|---|
| 某个能力**现在**是什么样 | **不看这里**，看 [`../architecture.md`](../architecture.md) / [`../module-map.md`](../module-map.md) |
| 某个能力**当初为什么**这么设计 | 找对应版本的工单，看「背景/目标/决策」段落 |
| 某个坑**当时怎么发现的** | 找对应版本工单的「执行记录」段落；结论已沉淀进 [`../progress.md`](../progress.md) |

## 清单

| 文件 | 版本 | 主题 |
|------|------|------|
| `cc_task.md` | V1 | 全链路初版实现 |
| `cc_task_v4.md` ~ `cc_task_v8.md` | V4–V8 | 策略/风控/回测逐版迭代 |
| `cc_task_v9.md` | V9.0 | Regime 六态 + 策略整合 + HMM/情绪可选模块 |
| `cc_task_v11.md` | V11.0 | 深度审计 F1–F13（13 项资金正确性缺陷） |
| `cc_task_v11_2.md` | V11.2 | 系统集成层（TradingGate / SystemLifecycle / 熔断） |
| `cc_task_v11_4.md` | V11.4 | 运行时验证（soak / 异常绝不 BUY / 监督审计） |
| `cc_task_v11_6.md` | V11.6 | 财务真相 + 迁移框架 + run.py 瘦身 |
| `cc_task_v11_7.md` | V11.7 | 测试网证据 + 状态模型 + 证据链 |
| `cc_task_v11_8.md` | V11.8 | Docker 生产运行时 + 主网就绪自检 |
| `cc_task_v12_1.md` | V12.1 | 工程收口（CI 绿灯 / 可空值契约 / 回归） |
| `cc_task_dashboard_usability.md` | V12.2 | Dashboard 易用性（operator-status / 状态字典 / `/ops`） |
| `cc_task_admin_config_console.md` | V12.4 | `/admin` 管理控制台（配置编辑 / 模式切换 / 回滚） |
| `cc_task_pi_small_capital.md` | V12 | Pi 生产就绪与小资金前置 |
| `cc_task_pi_root_quick_deploy.md` | V12.3 | Pi root 快速部署（外置 env / 局域网 / 重启自恢复） |
