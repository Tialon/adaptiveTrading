# adaptiveTrading V2.0 — SOL 专业量化交易系统

> 目标: SOL/USDT 自动化量化交易系统,基于资金流/订单流/趋势状态的动态高抛低吸,持续降低持仓成本。
>
> 原则: 规则策略实时交易 | AI 只分析与参数优化 | 风控优先 | 交易可解释 | 策略可回测。

## 架构(V2.0)

```
Binance WS/REST
   ↓
Market Engine
   ↓ (Redis Stream)
Analytics Engine      VWAP/CVD/Whale/OrderFlow/EMA
   ↓
Market Regime Engine  BULL/SIDEWAY/BEAR/PANIC
   ↓
Strategy Engine       Entry评分 / Exit / Grid / Trend
   ↓
Position Manager      成本/可卖/可买
   ↓
Risk Engine           百分比风控 + 异常保护
   ↓
Execution Engine      幂等下单 + 成交跟踪
   ↓
MySQL + Redis + AI Advisor(仅参数建议)
   ↓
Web Dashboard
```

## Phase 1 — 基础闭环
- [x] Position Manager 增强(position_snapshot 表)
- [x] Signal Engine(标准信号: score/reason[]/indicators, strategy_signal 表)
- [x] Risk 增强(百分比: max_position 40% / max_single 5% / daily_loss 5% / drawdown 15%)
- [x] Risk 异常保护(API/行情/价格瞬间波动 -> 暂停交易)
- [x] SOL 交易(SYMBOLS=SOLUSDT)

## Phase 2 — 策略优化
- [x] Entry 评分模型: 价格位置30% + VWAP偏离20% + CVD 20% + 买卖比15% + 量变15%(>=80 买 / 60-80 观察 / <60 禁止)
- [x] Exit 策略: 分批止盈(5%→20% / 10%→30% / 20%→50%) + 移动止盈 + 趋势退出(EMA死叉+CVD降+买压减)
- [x] Market Regime Engine(BTC/SOL 趋势+波动率+成交量+资金流)
- [x] Analytics 增强: Order Flow(buy/sell pressure, ratio, large order ratio), whale 事件全字段
- [x] Execution 幂等控制(client_order_id 唯一 + 信号去重)

## Phase 3 — AI 与回测
- [x] AI Advisor 改造: 只输出市场状态/参数建议(grid_spacing/position_ratio/risk), 30 分钟周期
- [x] strategy_performance 表(胜率/收益/回撤, 供 AI)
- [x] 回测系统: 历史 K线+成交回放 -> 收益率/胜率/最大回撤/夏普/交易次数
- [x] Redis Stream 消息总线(market event -> analytics/strategy/risk/monitor)
- [x] Dashboard 增强: 策略评分/原因, 市场环境, 风控, 持仓收益

## 执行要求(已遵循)
1. 保留 V1.0 分层架构,只增不重写
2. 每模块配单元测试
3. PAPER_TRADING 全程可运行
4. 所有策略输出标准 signal; 所有交易记录 reason
