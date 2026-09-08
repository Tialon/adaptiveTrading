# 测试网验证状态(V11.6 Operational Status)

> V11.6 收口交付。本文记录「真实币安测试网验证」**实际执行到哪一步、卡在哪、怎么续跑**,
> 与「怎么做」的 [testnet-runbook.md](testnet-runbook.md) 互补 —— 那本写操作步骤, 本文写结果与诚实结论。

## 1. 一句话结论

V11.6 的**代码 / 测试 / 运维手册 / 一键 soak runner 全部交付并钉死**, 但 7~24h 无人值守 soak
在本会话因**测试网不可达**未实际执行, 故就绪等级**仍为 L2**, 未虚报 L3。

## 2. 已实际验证(真实测试网)

| 项 | 结果 |
|----|------|
| 连通 / 交易规则(ExchangeInfo)/ 行情 / 鉴权(测试网 key) | ✅ 只读冒烟通过 |
| 真实下单 → 成交 → 账本 → 对账 全链路 | ✅ P0-1 验证通过(`test_v152_testnet_order_lifecycle.py`, `RUN_TESTNET_TRADING=1`) |
| 财务真相闭环(AccountLedger 仅纸面, 实盘锚定交易所对账) | ✅ 代码 + 测试钉死(P0-3 / P1-1) |

## 3. 未执行(诚实披露)

| 项 | 原因 |
|----|------|
| 7~24h 无人值守真实下单 soak | **网络不可达**: smoke test 报 `Cannot connect to host testnet.binance.vision:443`(环境因素, 非代码缺陷) |

## 4. 续跑步骤(网络恢复后)

```powershell
# 1. 确认测试网可达
curl https://testnet.binance.vision/api/v3/ping   # 期望返回 {}

# 2. 按 testnet-runbook.md §3 准备 soak 用 .env(真实下单: PAPER_TRADING=false + 测试网 key)

# 3. 一键 soak(启动 run.py + 周期采样 + 证据落盘 + 到点摘要)
python -m at01_common.soak --hours 7

# 4. 收证据: logs/soak-evidence.jsonl(逐行 health 快照)+ logs/adaptive.log + reports/ 每日复盘
```

## 5. 就绪等级判定

- **L2(运行时验证就绪)**: ✅ 达成(V11.4 长跑/故障注入/恢复审计 + V11.5 运维加固 + V11.6 财务真相闭环)。
- **L3(测试网无人值守实盘)**: ⬜ 待 §4 的真实 7~24h soak 在部署环境跑通后评估。
- **L4(主网无人值守)**: ⬜ 未达(需先过 L3, 且主网须显式 `LIVE_TRADING_CONFIRM=true` + 三重守卫放行)。

## 6. 与 runbook 的关系

- `testnet-runbook.md`: 测试网阶段「怎么做」(启动 / 监控 / 告警 / 证据 / 停机)。
- 本文: 测试网阶段「做到哪了、结论是什么、下一步怎么续」。
