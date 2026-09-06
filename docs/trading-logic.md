# 交易逻辑文档(V3.0)

> 每个信号/决策都可解释: score 0~100 + reason 列表 + indicators 快照

## 1. Entry 评分模型(买入)

5 维加权,`>= 80` 买入 / `60~80` 观察档(仅记录)/ `< 60` 禁止:

| 因子 | 权重 | 计算 | 满分条件 |
|------|------|------|---------|
| 价格位置 | 30% | (recent_high - price)/(high-low) | 价格在近期区间底部 |
| VWAP 偏离 | 20% | dip = max(0, -vwap_deviation)/2% | 低于 VWAP 2% |
| CVD | 20% | cvd_slope/0.5(须 cvd_rising) | CVD 强上升 |
| 主动买卖比 | 15% | delta_ratio/0.2 | 主动买占比 20%+ |
| 量能变化 | 15% | volume_ratio 分档 | ≥1.3 放量 |

## 2. Exit 策略(卖出)

三种退出方式,取最大卖出比例,带结构化标签:

| 标签 | 触发 | 动作 |
|------|------|------|
| `profit_target` | 盈利 5%→卖20% / 10%→卖30% / 20%→卖50% | 分批止盈 |
| `overbought` | 峰值回撤 ≥5% 且有浮盈 | 清仓 |
| `trend_reverse` | EMA死叉 + CVD降 + 买压减(三中二) | 清仓 |
| `risk_reduce` | 亏损中的趋势退出(附加) | 降风险 |

## 3. Decision Engine(融合)

```
net = Σ(买入票×regime买系数) - Σ(卖出票×regime卖系数)
```

- 策略权重: entry 1.0 / **exit 1.3(保命优先)** / grid 0.7 / trend 0.9
- regime 系数: 买 BULL×1.2 / BEAR×0.6 / **PANIC×0**;卖反向
- `|net| ≥ 0.35` 才行动, 否则 HOLD
- exit score≥85 强制 SELL
- 观察档信号不计票
- 权重可被 signal_result 绩效动态调整(win_rate 0.5 基准,±40%)

## 4. Alpha Score(机会评分,P1)

价格 30%(VWAP折价+区间位) + 资金流 25%(CVD+delta+大单偏向) +
趋势 20%(EMA+**SOL/BTC 相对强弱**) + 波动率 15%(0.3~1.5% 适中最优) +
情绪 10%(温和上涨+量比) = 0~100

## 5. Market Regime(环境)

| 环境 | 判定 | 策略调整 |
|------|------|---------|
| BULL | 多周期趋势向上+资金流入 | 趋势为主, 满仓配 |
| SIDEWAY | 中性/温和波动 | 网格高抛低吸, 半仓配 |
| BEAR | 趋势向下+资金流出 | 禁网格, 停止补仓, 1/4 仓 |
| PANIC | 振幅≥3%+放量+趋势崩 | 只减不加, 零买入 |

## 6. Grid 网格

- 启动价±2%,10 格,越界一个步长重设
- 下穿层价买入 / 回升层价+step/2 卖出
- 每层金额 = 单笔限额×2/格数

## 7. 风控链(审批顺序)

```
观察档拦截 → 熔断检查 → 异常保护暂停 → 价格有效 → 数量确定
→ 单笔限额(5%权益, 超限缩量) → 最小名义(10 USDT)
→ 买入: 持仓限额(40%权益, 剩余额度缩量) / 卖出: 持仓上限
```

异常保护(自动暂停 60s):
- 单笔价格波动 > 3%
- 行情静默 > 30s(WS 断)
- 连续 3 次执行失败

## 8. 执行幂等与状态机

- **幂等**: 同 `策略:标的:方向` 10 秒内去重
- **状态机**: `IDLE → ENTRY_PENDING → HOLDING → EXIT_PENDING → CLOSED → IDLE`
  - HOLDING 期间拒绝重复买入(闸门)
  - 纸面模式无挂单阶段,成交时自动补齐中间态

## 9. Portfolio 成本管理(核心思想)

> **卖出的目的不是减少仓位, 而是降低成本**

- 盈利卖出 → 已实现盈亏摊薄剩余持仓 → **保本价下降**
  - 例: 10 SOL@100, 卖 2@150(赚100) → 剩 8 个保本价 87.5
- 目标仓位随 regime 调整(BULL 100%/SIDEWAY 50%/BEAR 25%/PANIC 0)
- 每次成交记录成本事件(成本曲线,内存最近 200 条)

## 10. signal_result(信号质量闭环)

信号执行后注册跟踪,每 60s 更新:
- `future_profit`: 最新相对盈亏(SELL 信号反向)
- `max_profit / max_drawdown`: 窗口内极值
- 1 小时窗口结束 `final=True`

用途: Decision Engine 动态调权 + AI Advisor 学习输入。

## 11. AI Advisor(边界)

**不做任何买卖决策**。30 分钟周期,输入(行情快照/近 20 单/策略绩效/持仓),
输出 JSON: `market_regime / grid_spacing / position_ratio / risk_level / warnings / summary`。
