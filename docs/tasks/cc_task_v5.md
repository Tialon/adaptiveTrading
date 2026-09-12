# adaptiveTrading V5 — 资产管理执行层强化

> 前提: V4 已落地资产管理框架; V5 解决代码级审查发现的关键缺口:
> **实际交易链路是否真的按资产管理模型执行**

## 代码审查发现的缺口(本次修复)

| 缺口 | 风险 | 修复 |
|------|------|------|
| 卖出数量按总持仓计算(position_provider 返回总仓) | exit 清仓信号卖出量=总仓,实盘下币安已成交,纸面回滚失效 | 卖出链路三层修复(见 P1) |
| Signal 无 bucket 概念 | 策略层不知道双仓 | Signal.bucket 字段 |
| 敞口只看 regime+confidence | 高波动/BTC 破位仍高仓 | risk_adjustment_factor |
| 单笔固定 5% | 牛市仓位建立过慢 | dynamic_trade_limit |

## P0 — 动态风险(已完成)

### StrategyContext(strategy_base.py)
- [x] 组合上下文: regime/置信/敞口/目标敞口/core_qty/trade_qty/cash/equity/回撤档/Alpha
- [x] 策略从"只看行情"升级为"看行情+看资产"

### Dynamic Risk Allocation(risk_allocation.py)
- [x] risk_adjustment_factor: 波动率(>1.5%×0.8/>3%×0.6) + BTC(跌4%×0.8) + 回撤分级
- [x] 敞口公式: exposure = regime × confidence × risk_factor(下限 0.4)

### Dynamic Trade Limit(risk_sizing.py)
- [x] 牛市 5-10% / 震荡 3-5% / 熊市 0-3% 单笔限额, 回撤/波动再收紧

### Portfolio Backtest(at70_backtest/backtest_portfolio.py)
- [x] 模拟现金 → 核心仓 → 交易仓 → 动态敞口再平衡 → 收益
- [x] SOL Buy-Hold 基准对比 + core/trade 贡献分解
- [x] 曲线: equity / exposure / core / trade / cash
- [x] 交易仓高抛低吸(涨3%卖30%), 卖交易仓永不动核心仓

## P1 — 卖出 Bucket 硬保护(已完成, 三层)

1. **策略层**: position_provider 只暴露交易仓(卖出策略不可见核心仓)
2. **信号层**: Signal.bucket="trade", 卖出信号数量基于交易仓
3. **执行层**: 下单前闸门——卖出数量以交易仓可用量封顶, 不足缩量或丢弃
   (替代 V4 的"成交后纸面回滚", 实盘安全)

## P1 — AI Parameter History(已完成)

- [x] ai_parameter_history 表: 参数名/旧值/新值/原因/效果(24h PnL 追踪)
- [x] AI 建议变化才记录, 可回答"AI 有没有帮助"

## P2 — 待办

- [ ] StrategyContext 完整注入各策略 evaluate(context) 签名(当前部分策略仍用 MarketAnalytics)
- [ ] AI 建议自动应用(当前仅记录)
- [ ] 组合回测接入真实 RegimeEngine(当前为 EMA 近似)
- [ ] Funding Rate / 链上数据 / DEX Flow

## 验证

- 184/184 测试(+16 V5)
- E2E: 卖出闸门/评分定仓/双仓记账 全链路
