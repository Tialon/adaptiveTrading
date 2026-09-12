# adaptiveTrading V7 — Backtest Realism(回测真实化)

> 前提: V6 修完账务正确性; V7 解决"回测里的策略是否就是实盘里的策略"。

## P0(全部完成)

### 1. 移除回测内嵌隐藏交易策略 ✅
- 删除 "+3%/-3%/15%/5%" 简化交易逻辑
- 回测现在驱动**真实 StrategyEngine + DecisionEngine + PositionSizer**
  (与实盘完全同一代码, position_provider 同样只暴露交易仓)

### 2. Decision 数量解耦 ✅
- Decision.quantity/quote_amount 降级为**建议参考值**(注释明确)
- 最终数量由 PositionSizer + PortfolioAllocator 计算

### 3. 未知策略权重安全 ✅
- `weights.get(strategy, 0.5)` 静默回退 → 警告并跳过计票
  (交易系统宁可停止也不静默用错参数)

### 4. 滑点/价差执行模型 ✅
- SlippageModel: BUY×(1+bps) / SELL×(1-bps), 可敏感性(0/5/10/20bps)

### 5. 次bar执行(杀 look-ahead)✅
- NextBarExecutor: t 收盘信号 → t+1 开盘成交(含滑点)
- 不再用同一根 close 同时判断和成交

### 6. BTC asof 对齐 ✅
- AsOfJoiner: <= sol_ts 最近值, 数据龄检查(超龄标记)
- 替代精确 timestamp match 的 stale fallback

### 7. interval 正确分页 ✅
- at01_common/timeframe.py 统一换算(bars_per_day/year,
  全系统唯一来源), days×bars_per_day 目标量

### 8. 测试与不变量 ✅
- 217/217 测试(+15 V7)
- no-lookahead / asof 单调游标 / bucket 卖出边界 /
  equity 恒等式 / 未知策略拒绝 / 滑点方向

## 验证(真实 SOL 1 天数据, 滑点敏感性)

| 滑点 | 收益 | 超额 | 交易数 |
|------|------|------|--------|
| 0bps | +0.18% | -1.83% | 48 |
| 10bps | +0.01% | -1.97% | 52 |
| 20bps | -0.21% | -2.19% | 53 |

**关键洞察: 48+ 笔交易在 10bps 滑点下吃掉全部利润。**
高频网格摩擦成本是当前最大亏损源 —— P1 参数优化(网格频率/阈值)
有了量化的改进目标。对账恒平衡(balanced=True)。

## 诚实结论

真实管线 + 滑点后, 策略在上涨日跑输 Buy-Hold 约 2%:
1. 网格高频交易的滑点+手续费摩擦(48 笔)
2. 交易仓在上涨段被网格卖出(无法吃到趋势)

## P1(下一步)

- [ ] 参数敏感性网格(网格频率/止盈阶梯/entry 阈值 70~90)
- [ ] Strategy Attribution(谁赚钱: ENTRY/EXIT/GRID/TREND × Regime)
- [ ] Live/Backtest 共用 Analytics Builder(替代合成 tick)
- [ ] Walk-Forward OOS 统计
- [ ] AI Parameter Experiment(参数 A/B 效果验证)
- [ ] Sortino / Profit Factor / Calmar
