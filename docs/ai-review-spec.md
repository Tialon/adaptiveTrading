# AI Review Package 规格

> **这份文档定义 `review/YYYY-MM-DD/` 里每个文件的结构与语义。**
> 生成器: `at70_journal/ai_review.py`。接口: `GET /api/ai-review/latest`。
>
> 相关: [`product/unattended-operation.md`](product/unattended-operation.md)、
> [`trading-logic.md`](trading-logic.md)。

---

## 0. 红线(不可越界)

```
AI 可以分析 · 可以复盘 · 可以提出优化建议
AI 不可以直接下单
```

```
AI → Review → Analysis → Proposal → Human approval → Strategy Version
```

禁止的路径:

```
AI → BUY
AI → SELL
```

这条红线**写在产物本身里** —— `ai_review.md` 顶部有固定声明, 读这份文件的人或 AI
都看得到。优化器只产 `active=False` 的 proposal
(`at85_optimizer/`), 激活必须人工调用。

---

## 1. 目录结构

```
review/
  YYYY-MM-DD/
    summary.json           周期 + 系统状态 + 绩效摘要
    trades.json            当日平仓交易
    signals.json           当日策略信号(含未被采纳的)
    risk.json              当日风控事件
    execution.json         当日订单执行事件
    market_regime.json     按市场环境聚合的表现
    performance.json       绩效指标(含 HODL 对标状态)
    anomalies.json         问题清单 + 研究候选
    strategy_version.json  当日生效的策略版本与参数
    ai_review.md           给人读的那一份
```

输出目录由 `AI_REVIEW_DIR` 配置(默认 `review`), 已 gitignore。

**为什么按天分目录而不是一个库**: AI 的输入应该是**不可变的一段历史**。
库会一直变, 今天问「昨天赚了多少」得到的答案明天可能不同 —— 那对复盘是致命的。

---

## 2. 要能回答的问题

任务书原文:

1. 今天赚了还是亏了? 为什么?
2. 哪些信号有效? 哪些信号误判?
3. 哪个市场环境表现差?
4. 执行有没有问题?
5. 风控有没有误杀?
6. 策略是否过度交易?
7. 与 HODL 比怎么样?
8. 哪些参数值得研究?

对应关系:

| 问题 | 数据来源 |
|------|----------|
| 1 | `performance.json` + `trades.json` |
| 2 | `signals.json`(`status=rejected` 即未被采纳) |
| 3 | `market_regime.json`(**按盈亏升序, 表首即最差**) |
| 4 | `execution.json`(`UNKNOWN` / `RECOVERY` 事件) |
| 5 | `risk.json` + `anomalies.json` 的 `signals_rejected` |
| 6 | `anomalies.json` 的 `possible_overtrading` |
| 7 | `performance.json` 的 `benchmark` |
| 8 | `anomalies.json` 的 `recommendation_candidates` |

---

## 3. 各文件字段

### summary.json

```json
{
  "period": {"date": "2026-09-12", "start_ts": …, "end_ts": …, "timezone": "local"},
  "system_status": {
    "symbol": "SOLUSDT",
    "stability": {
      "ws_reconnects": 2, "auto_recoveries": 3, "degradations": 0,
      "kills": 0, "human_interventions": 0, "errors": 1,
      "trades": 8, "startups": 1, "total": 42, "by_kind": {}
    },
    "generated_at": 1789200000.0
  },
  "performance": { … },
  "strategy_version": { … },
  "anomaly_count": 2
}
```

`stability` 与页面「今日系统复盘」**同源**(`operator_log.day_summary()`),
两处不会打架。

### performance.json

```json
{
  "trades": 8, "wins": 5, "losses": 3, "win_rate": 0.625,
  "total_pnl": 121.0, "gross_profit": 180.0, "gross_loss": 59.0,
  "profit_factor": 3.05, "avg_holding_seconds": 4210.0,
  "best_trade": 60.0, "worst_trade": -21.0,
  "max_drawdown_pct": 0.73,
  "benchmark": {"baseline": { … }} | null,
  "by_strategy": [{"strategy": "trend_swing", "trades": 5, "win_rate": 0.8, "pnl": 142.0}]
}
```

- `profit_factor` 在**没有亏损**时为 `null`(「不适用」), 不是 0 也不是无穷大;
- `benchmark` 为 `null` 表示**尚未建立 HODL 基准** —— 此时不给 Alpha,
  也不用其它数字代替(`ai_review.md` 会明写「尚未建立」)。

### market_regime.json

```json
{"market_regimes": [{"regime": "PANIC", "trades": 2, "win_rate": 0.0, "pnl": -30.0}]}
```

**按 `pnl` 升序** —— 表首即当日表现最差的环境。

### anomalies.json

```json
{
  "anomalies": [
    {"kind": "signals_rejected", "text": "5 个信号未被采纳", "count": 5, "hint": "…"}
  ],
  "recommendation_candidates": [{"from": "signals_rejected", "topic": "…", "count": 5}]
}
```

`kind` 取值:

| kind | 触发条件 |
|------|----------|
| `signals_rejected` | 有信号未被采纳 |
| `order_unknown` | 出现订单状态未知(超时/未确认) |
| `order_recovery` | 出现订单恢复动作 |
| `possible_overtrading` | 当日成交 > 20 次 |
| `gave_back_profit` | 曾浮盈可观但最终亏损 |
| `kill_switches` / `degradations` / `errors` / `ws_reconnects` | 稳定性计数非零 |
| `risk_events` | 当日有风控事件 |

> `recommendation_candidates` 是**候选研究课题**, 不是结论。
> 「值得研究」与「应该改」是两件事, 后者必须人工判断。

### strategy_version.json

```json
{"version": "0.1.0-20260912", "activated": false,
 "note": "startup baseline", "params": { … }, "created_at": "…"}
```

`activated=false` 表示这是**启动基线**(服务启动那一刻的参数快照),
不是人工激活过的版本。两种都会出现在包里, 由 `activated` 区分。

---

## 4. 脱敏(硬约束)

**包会被导出、被喂给 AI、被贴进对话** —— 密钥绝不能在里面。

所有字符串过 `at01_common/operator_events.py::scrub_detail`:

1. **按键名**: 含 `KEY` / `SECRET` / `TOKEN` / `PASSWORD` / `PASSPHRASE` 的键 → `<masked>`;
2. **按文本形态**: 自由文本里形如 `NAME_KEY=value` / `token: value` 的片段 → 掩码。

第 2 条是必要的: 密钥常被拼进 `reason` / `detail` 这类自由文本, 只按键名过滤会漏。

有测试植入假密钥(`api_key=LEAKEDKEY123`、`BINANCE_API_SECRET=LEAKEDSECRET456`)
断言其不出现在产物的任何文件里。

---

## 5. 生成时机

- **每日自动**: `run.py::_daily_report_loop` 每 24h 生成一次, 与每日复盘报告**同批**。
  数据只算一次, 两种读物各取所需:
  - 人 → `reports/<日期>.md` 的「今日系统复盘」段落;
  - AI → `review/<日期>/`。
- **手动**: `GET /api/ai-review/latest?day=YYYY-MM-DD` 实时构建指定日期(**不写文件**)。

> 报告是给时间点留档的, 所以文件读取型接口**不在请求里现算** ——
> 让 HTTP 触发重算会让「今天」在两次请求间悄悄改变含义。

---

## 6. 接口

| 接口 | 返回 |
|------|------|
| `GET /api/ai-review/latest` | 最近一份包(`summary` + `markdown`) |
| `GET /api/ai-review/latest?day=YYYY-MM-DD` | 实时构建该日包(不写文件) |
| `GET /api/reports/daily[?day=]` | 人读日报 Markdown |
| `GET /api/reports/trades[?day=]` | 当日交易明细 |
| `GET /api/reports/system[?day=]` | 当日稳定性汇总 |

全部**只读**、无需令牌、无任何写路径。
