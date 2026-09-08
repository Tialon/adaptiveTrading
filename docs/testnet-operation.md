# 测试网验证状态(V11.7 Operational Status)

> V11.7 收口交付。本文记录「真实币安测试网验证」**实际执行到哪一步、卡在哪、怎么续跑**,
> 与「怎么做」的 [testnet-runbook.md](testnet-runbook.md) 互补 —— 那本写操作步骤, 本文写结果与诚实结论。

## 1. 一句话结论

V11.7 的**代码 / 测试 / 运维手册 / 一键 soak runner / 测试网真实执行闸门全部交付并钉死**;
本会话测试网**可达**, 已真实执行**一次 BUY/SELL 订单生命周期闭环并 PASSED**; 但 7~24h 无人值守
soak 需真实挂机, 本会话未执行, 故就绪等级按 P1-9 规则**仍为 L2**, 未虚报 L3。

## 2. 已实际验证(真实测试网)

| 项 | 结果 |
|----|------|
| 连通 / 交易规则(ExchangeInfo)/ 行情 / 鉴权(测试网 key) | ✅ `ping`/`time`/`exchangeInfo`/`account` 均返回(真实 `testnet.binance.vision`) |
| 订单生命周期(no-fill) | ✅ PASSED: LIMIT BUY no-fill → NEW → cancel → CANCELED, myTrades 空, SOL 余额不变 |
| real BUY | ✅ PASSED: MARKET BUY 0.072 SOL @ 103.4 FILLED → Position/PositionLot/Order/OrderFill 落库 |
| real SELL | ✅ PASSED: MARKET SELL 0.072 SOL @ 103.39 FILLED → 平仓 → SellAllocation 落库 → 持仓归零 |
| 财务真相对账(单笔) | ✅ PASSED: 交易所真相 SOL 余额交叉对账(买入净增≈fill_qty, 卖出回基线) |
| 三重主网守卫 | ✅ 全程在位(配置 `BINANCE_TESTNET=true` + 客户端 `testnet=True` + base_url 含 "testnet") |

> 执行方式: `RUN_TESTNET_TRADING=1 pytest tests/testnet/test_v152_testnet_order_lifecycle.py`(2 passed in 27.37s);
> `.env` 无主网 key。

## 3. 未执行(诚实披露)

| 项 | 原因 |
|----|------|
| 7~24h 无人值守真实下单 soak | **需真实挂机 7/24 小时**(本会话时长不可达, 非代码缺陷); 状态 `READY_TO_RUN → NOT_EXECUTED` |

## 4. 续跑步骤

```powershell
# 1. 确认测试网可达
curl https://testnet.binance.vision/api/v3/ping   # 期望返回 {}

# 2. 按 testnet-runbook.md §3 准备 soak 用 .env(真实下单: PAPER_TRADING=false + 测试网 key)

# 3. 一键 soak(启动 run.py + 周期采样 + 证据落盘 + 到点摘要)
python -m at01_common.soak --hours 7

# 4. 收证据: logs/soak/<run_id>/{metadata.json,evidence.jsonl,summary.json,runtime.log}(V11.7 P0-4 可复现元数据)
```

## 5. 就绪等级判定

- **L2(运行时验证就绪)**: ✅ 达成(V11.4 长跑/故障注入/恢复审计 + V11.5 运维加固 + V11.6 财务真相闭环 +
  V11.7 证据硬化 + 真实订单闭环 PASSED)。
- **L3(测试网无人值守实盘)**: ⬜ 待 §4 的真实 7h soak PASS 后评估(单次 BUY/SELL 不升 L3)。
- **L4(主网无人值守)**: ⬜ 未达(需先过 L3, 且主网须显式 `LIVE_TRADING_CONFIRM=true` + 三重守卫放行)。

## 6. 与 runbook 的关系

- `testnet-runbook.md`: 测试网阶段「怎么做」(启动 / 监控 / 告警 / 证据 / 停机)。
- 本文: 测试网阶段「做到哪了、结论是什么、下一步怎么续」。
