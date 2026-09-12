# docs/ — 文档索引

> 17 份文档，别从头翻。**先看下面这张「我想知道 X → 读哪份」表。**
>
> 每份文档标注了**状态**：
> - 🟢 **当前态** —— 描述代码现在长什么样，与代码同步维护
> - 🟡 **状态快照** —— 某个时间点的结论（如「测试网验证做到哪」），**会过期**
> - ⚪ **历史存档** —— 记录当时发生过什么，**故意不改写**

---

## 按问题找文档

| 我想知道… | 读这份 | 状态 |
|-----------|--------|------|
| 系统整体怎么分层、一次成交怎么流转、**四套状态机什么关系** | [`architecture.md`](architecture.md) | 🟢 |
| 某个文件/模块负责什么 | [`module-map.md`](module-map.md) | 🟢 |
| **交易逻辑本身**：Entry 评分怎么算、Exit 怎么分批、融合决策怎么加权 | [`trading-logic.md`](trading-logic.md) | 🟢 |
| **现在能不能下单 / 在哪个模式 / 怎么切模式** | [`operating-modes-manual.md`](operating-modes-manual.md) §0.5 速查 | 🟢 |
| 怎么启动、排障、看 API | [`runbook.md`](runbook.md) | 🟢 |
| 改数据表结构要同步哪几处 | [`database-migration.md`](database-migration.md) | 🟢 |
| 为什么要用内存 Metrics 而不落库 | [`metrics-persistence.md`](metrics-persistence.md) | 🟢 |
| 项目做到哪一步了、就绪等级 | [`production-readiness.md`](production-readiness.md) | 🟡 |
| 这个版本交付了什么 | [`progress.md`](progress.md) | 🟢 |
| 更早的版本呢 | [`progress-archive.md`](progress-archive.md) | ⚪ |

### 审查报告

| 我想知道… | 读这份 | 状态 |
|-----------|--------|------|
| 有没有绕过闸门/风控的下单路径、幂等够不够、失败会不会下单 | [`audits/trading-path-audit.md`](audits/trading-path-audit.md) | 🟢 |

### 按运行模式找

| 模式 | 文档 |
|------|------|
| 通用（三种模式都适用） | [`operating-modes-manual.md`](operating-modes-manual.md) |
| 测试网真实执行 / 无人值守 soak | [`testnet-runbook.md`](testnet-runbook.md) + [`testnet-operation.md`](testnet-operation.md)（🟡 做到哪的诚实结论） |
| 主网实盘 | [`mainnet-runbook.md`](mainnet-runbook.md) → [`mainnet-readiness.md`](mainnet-readiness.md) → [`mainnet-prestart-checklist.md`](mainnet-prestart-checklist.md) |

> ⚠️ **主网三份的顺序不能颠倒**：先读运行手册理解纪律 → 过就绪复审（人工 go/no-go）→
> 每次启动前过 checklist。**自动化不得自行批准首笔主网交易。**

### 按部署载体找

| 载体 | 文档 |
|------|------|
| Docker 容器 | [`docker-deployment.md`](docker-deployment.md) |
| 树莓派 arm64 | [`raspberry-pi-deployment.md`](raspberry-pi-deployment.md) |
| 部署资产本身（`deploy/` 里有什么） | [`../deploy/README.md`](../deploy/README.md) |

---

## 归档目录（别当现状读）

| 目录 | 内容 | 注意 |
|------|------|------|
| [`tasks/`](tasks/) | 18 份历史工单 `cc_task_*.md` | ⚠️ 里面是**当时的旧包名**，映射表见 [`tasks/README.md`](tasks/README.md) |
| [`../design/`](../design/) | V1.0 时期设计图 | ⚠️ 已与代码严重脱节，当前架构图以 [`architecture.md`](architecture.md) 为准 |

---

## 冲突时以谁为准

**文档与代码冲突 → 以代码为准。** 各事实的权威位置：

| 事实 | 权威位置 |
|------|----------|
| 数据表结构（28 张） | `at01_common/models.py` |
| 配置项与校验规则 | `at01_common/settings.py`（含 `validate()`） |
| 数据库 schema 版本 | `at01_common/database.py::SCHEMA_VERSION` |
| 交易许可（能不能买卖） | `at50_risk/trading_gate.py::TradingGate` |
| 启动守卫顺序 | `at01_common/wiring.py::wire_system()` |
| 后台任务与周期 | `run.py` 的 `supervisor.spawn(...)` 调用点 |

> 本仓库的文档曾多处滞后于代码（版本号、表数、测试数、甚至一处「八维」实为九项）。
> **发现文档与代码不符时，改文档**——并在提交信息里写清哪里不符、依据是什么。

---

## 维护约定

- 图表用 **Mermaid**（GitHub 原生渲染、可 diff）。改完跑一次语法校验：
  ```bash
  python scripts/check_docs_mermaid.py docs/
  ```
  Mermaid 的坑：语法错**不报错**，只渲染成空白；更隐蔽的是「语法合法但布局崩掉」。
- 结构/依赖关系图用 **ASCII**（分层图那种）—— 它们永远渲染，在终端和 git diff 里都能读。
- 文档交叉链接用相对路径；改动标题时记得同步锚点。
