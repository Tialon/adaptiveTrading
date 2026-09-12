# design/ — 设计图（⚠️ 历史存档）

> **这里的图是 V1.0 时期（2026-09-06）的产物，已与当前代码严重脱节，不要作为理解系统的依据。**

## 内容

| 文件 | 时期 | 说明 |
|------|------|------|
| `系统设计图.png` | V1.0 | 最初的系统设计图 |
| `adaptiveTrading流程图.drawio` | V1.0 | 流程图（可用 draw.io 打开编辑） |
| `自适应交易系统.md` | V1.0 | 6 个方框的流程图：Market → Analytics → Strategy → Risk → Execution → MySQL+Redis |

## 为什么脱节

那张图停留在 **V1.0** —— 当时系统只有「行情/分析/策略/风控/执行」五个引擎、存 MySQL + Redis。
此后 12 个版本的迭代加了这些东西，图上**一个都没有**：

- 双仓模型（核心/交易/现金三桶）+ 组合成本管理
- 决策融合引擎（多策略加权 → 唯一 BUY/SELL/HOLD）
- 统一交易闸门 `TradingGate`（六维）+ 顶层生命周期状态机（10 态）
- 资金级熔断（Equity/Position/Cash 三向漂移）
- 对账矩阵 + 交易所真相对账 + FIFO Lot 会计 + 账户账本
- OpenAI 类的 AI 参数顾问（只建议、不下单）
- 回测（真实策略管线 + 次 bar 执行 + 滑点）、Walk-Forward、参数优化
- Docker 生产运行时 + 主网就绪自检 + 测试网执行闸门
- Web 面板 / 管理控制台 / 部署自检页
- 存储从 MySQL+Redis 改为 **SQLite 单机（WAL）**，Redis 默认关闭

## 当前架构图看哪里

→ [`docs/architecture.md`](../docs/architecture.md)

那份文档里的图用 **Mermaid** 写（GitHub 原生渲染、可 diff、可随代码一起改），
分三张：分层总图 / 成交时序图 / 三套状态机关系图。

新图不再放 `design/` —— 放在 `docs/` 里跟文档同源，才能保证「代码改了图也改」。
这个目录**只作为历史存档保留**。
