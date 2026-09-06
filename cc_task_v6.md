# adaptiveTrading V6 — Correctness First

> 原则: **不新增复杂策略。先保证现有资产管理系统的代码正确、可测试、可回测。**

## 代码审查确认的 4 个真实 Bug(本次全部修复)

| Bug | 影响 | 修复 |
|-----|------|------|
| 策略名 buy/sell vs 权重键 entry/exit 不一致 | entry=1.0/exit=1.3 权重失效,实际掉 0.5 默认值 | P0-1 |
| `"buy+decision"` 融合名导致 on_fill 找不到原策略 | 成交回调静默丢失 | P0-2 |
| 回测 trade PnL 用 core_cost 计算 | 交易仓盈利严重虚高 | P0-3 |
| 回测交易仓初始化是 `pass` | 双仓模型从未完整运行 | P0-4 |
| 回测 risk factor 固定 btc=0/neutral/drawdown=0 | 回测与实盘风险模型不一致 | P0-5 |
| 历史数据上限 1000 根(16.7h) | "7天回测"名不副实 | P0-6 |
| 回测自写简化 Regime | Live ≠ Backtest | P0-7 |
| Sharpe 固定按 1m 年化 | 换 interval 即错 | P0-7 |

## P0 修复清单(全部完成)

- [x] P0-1 Strategy Identity: StrategyType 枚举(entry/exit/grid/trend),
      策略 name/装配表/权重表/能力声明统一, capability(can_buy/can_sell)
- [x] P0-2 Decision Fill Callback: Signal.source_strategy, 融合信号
      strategy="decision" + 回调路由回源策略
- [x] P0-3 Portfolio Ledger: at60_risk/risk_ledger.py 双仓独立加权成本
      (含费), realized_pnl 精确, 现金逐笔推演
- [x] P0-4 交易仓真实生命周期: 建立/高抛(3%)/低吸(-3%回补)/再买卖
- [x] P0-5 Backtest Risk 一致: BTC 历史 K线对齐 + 组合回撤真实输入
      risk_adjustment_factor(与实盘共用)
- [x] P0-6 分页历史数据: days=3 实拿 4320 根(验证)
- [x] P0-7 共用 MarketRegimeEngine / periods_per_year(interval) 年化 /
      Benchmark 统一执行价起点
- [x] P0-8~10 测试四层: Unit(既有) + Invariant(核心仓不可交易卖/
      PANIC禁买/敞口上限) + Accounting(账本对账/双仓成本独立/漂移检出) +
      Regression(bull/bear/sideway/crash 固定数据集)

## 验证结果

- **202/202 测试通过**(+18 金融正确性)
- 回测对账恒平衡(balanced=True, P0-9 不变量)
- 真实数据(3天/4320根/BTC对齐): core +147.18 / trade -27.32 /
  17 次再平衡 / 37 笔 —— **数字不再虚高,可作为调优起点**

## 诚实结论(回测显示的问题)

修正账目后,策略 3 天跑输 Buy-Hold 1.57%。这是真实结果:
- 交易仓高抛低吸(-27.32)在上涨段提前卖出
- 这正是 P1 阶段要优化的"盈利核心"(而非继续堆功能)

## P1(下一步, 修完 P0 后)

- [ ] 参数敏感性测试(买入阈值 70/75/80/85/90 网格, 防过拟合)
- [ ] Portfolio Context 完整注入策略签名
- [ ] BTC/SOL 相关性 / 波动率 regime
- [ ] Benchmark 增加 DCA
- [ ] Walk-Forward 接入 Portfolio 模型
