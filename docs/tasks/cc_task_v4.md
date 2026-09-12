# adaptiveTrading V4 — SOL 主动资产管理系统

> 目标: 2 万人民币本金 / SOL 单币 / 长期盈利
> 从「高级交易机器人」升级为「主动资产管理系统」:
> 判断市场状态 → 决定风险暴露 → 决定仓位 → 决定买卖 → 优化长期收益

## P0 — 资产管理核心(已完成)

### Portfolio Allocation Engine(at60_risk/risk_allocation.py)
- [x] regime × confidence → target_exposure
- [x] 敞口表: strong_bull 0.90 / BULL 0.75 / SIDEWAY 0.50 / BEAR 0.25 / PANIC 0.10
- [x] 置信度加权: 低置信向 50% 收敛
- [x] 双仓分配: 核心 70% / 交易 30%
- [x] 再平衡: 敞口偏离 >5% 容忍带触发
- [x] 熊市/恐慌停止加仓

### 核心仓/交易仓双仓(position_bucket 表)
- [x] BucketPositionManager: 卖交易仓不动核心仓
- [x] 跨仓保护: 交易仓不足拒绝跨仓卖出(REJECTED, 总账回滚)
- [x] 显式核心仓减仓(仅 allocation 引导)
- [x] 启动加载 + 成交落库

### Position Size 模型(at60_risk/risk_sizing.py)
- [x] position_ratio = 基准10% × Alpha × regime系数 × 回撤档 × 决策置信
- [x] 牛市 85 分 ≈ 10%权益;熊市 85 分 ≈ 2%(regime 0.25)
- [x] 三重约束: 单笔 5% 上限 / 敞口缺口 / 最小名义
- [x] 接入 run.py 信号管道(替代固定金额)

### 分级回撤风险(at60_risk/risk_tiered.py)
- [x] 五档: 10% reduce_trade / 20% reduce_exposure / 30% stop_add /
       40% defensive / 50% emergency(接熔断)
- [x] 每档收紧 size_factor / exposure_factor / grid / add_position
- [x] 滞后降档(阈值-2%), 防抖动
- [x] risk-loop 评估 + 档位事件落库

## P1 — 复盘与验证(已完成)

### Trading Journal(decision_log 表)
- [x] 每次决策(含 HOLD)完整上下文: 时间/价格/市场状态(置信)/Alpha/
       core/trade 仓位/现金/权益/原因/指标快照
- [x] 挂在 StrategyEngine 融合决策点

### Benchmark 回测
- [x] SOL Buy-Hold 基准 + 超额收益(excess_return)
- [x] Portfolio 曲线: exposure / cash / position

## P2 — AI 节奏调整(已完成)

- [x] AI 周期: 30 分钟 → 每日(86400s), 匹配日线级参数优化节奏

## P3 — 待办

- [ ] AI 建议自动应用到策略参数(当前仅展示)
- [ ] 回测纳入 Allocation/双仓完整模型(当前为简化交易模型)
- [ ] 核心仓自动再平衡执行(当前输出建议, 人工/半自动确认)
- [ ] Funding Rate / 链上数据源
- [ ] 多币种扩展

## 关键参数(2 万本金配置)

```yaml
allocation:
  strong_bull: 0.90    # 牛市 80-90%
  bull: 0.75
  neutral: 0.50
  bear: 0.25           # 熊市 20-30%
  panic: 0.10
  core_ratio: 0.70
  trade_ratio: 0.30
  rebalance_tolerance: 0.05

sizing:
  base_ratio: 0.10     # 满分信号买 10% 权益
  max_single: 0.05

drawdown_tiers:
  10%: reduce_trade (size x0.7)
  20%: reduce_exposure (exposure x0.7)
  30%: stop_add (size x0)
  40%: defensive (exposure x0.3, 停网格)
  50%: emergency (熔断)
```
