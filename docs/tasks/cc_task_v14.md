# adaptiveTrading V13 — 无人值守产品化与低摩擦运维任务书

## 目标

在当前 V12.9 工程基础上，不新增交易策略，不改变核心风控安全契约。

本轮目标是：

> 把 adaptiveTrading 从“工程师可操作的自动交易程序”升级为“普通用户配置必要 Key 后即可长期无人值守运行的交易产品”。

核心原则：

```text
安全由机器自动完成
运维由机器自动完成
状态由机器解释
异常由机器分类
恢复由机器自动尝试
只有真正需要人类承担资金/策略责任的事项才要求人工确认
```

禁止为了降低操作复杂度而删除或弱化：

* TradingGate
* 风控
* 对账
* 幂等
* kill switch
* mainnet safety
* testnet safety
* fail-closed

本轮不是“删除安全”，而是：

> **把安全检查从“要求用户操作”变成“系统自动执行”。**

## P0 产品原则重新定义

### 用户只需要确认 5 类事情

默认情况下，用户只需要明确确认：

1. API Key / Secret 已配置
2. 交易模式
3. 交易资金范围 / 风控边界
4. 策略版本
5. 是否允许进入真实资金运行

除此之外：

* 不要求用户手工执行十几个检查命令
* 不要求用户理解 `PAPER_TRADING`
* 不要求用户理解 `BINANCE_TESTNET`
* 不要求用户理解 `RUN_TESTNET_TRADING`
* 不要求用户理解内部状态机
* 不要求用户手工检查 Docker
* 不要求用户每天查看日志
* 不要求用户手工执行 reconciliation
* 不要求用户手工恢复普通 transient error
* 不要求用户自己判断系统是否健康

这些全部由系统完成。

## P0 重新设计“启动 / 部署 / 运行”模型

将当前“部署检查”从：

```text
用户执行检查
↓
用户看到 PASS/WARN/BLOCKED
↓
用户判断
↓
用户处理
```

升级为：

```text
系统启动
↓
自动检查
↓
自动修复可修复问题
↓
自动重试 transient error
↓
无法自动解决 → 明确告诉用户原因
↓
只有高风险事项才要求人工确认
↓
READY
↓
无人值守运行
```

### 将检查分成 3 类

#### A. 系统自动处理

例如：

* 网络暂时失败
* Binance API timeout
* WS 断线
* REST retry
* 数据预热不足
* 数据延迟
* transient reconciliation failure
* 单个后台任务异常
* Docker restart
* 服务重启
* 普通 DEGRADED
* RECOVERY

原则：

> 用户不应该被要求处理这些事情。

系统自动：

```text
retry
backoff
reconnect
reconcile
recover
degrade
resume
```

并记录全过程。

#### B. 系统自动阻止，但无需人工确认

例如：

* API key 缺失
* API key 无效
* Binance 不可访问
* 数据异常
* 对账异常
* 权益异常
* 风控超限
* 系统状态不允许交易

系统直接：

```text
停止交易
保持服务运行
展示原因
自动恢复尝试
```

而不是让用户点击十几个按钮。

#### C. 必须人工确认

只保留真正需要人类承担责任的事项：

```text
进入主网真钱模式
修改核心风控参数
激活新的策略版本
解除高风险 KILL
改变资金规模
重新启用被重大风险冻结的系统
```

这些可以保留确认。

## P0 模式模型简化

内部可以继续保留：

```text
PAPER
TESTNET
LIVE
```

但用户界面必须只有：

```text
模拟
测试
实盘
```

不要把：

```text
paper_testnet
live_testnet
live_mainnet
paper_mainnet
PAPER_TRADING
BINANCE_TESTNET
RUN_TESTNET_TRADING
LIVE_TRADING_CONFIRM
MAINNET_API_SCOPE_CONFIRMED
```

暴露给普通用户。

高级诊断页面可以显示这些字段。

普通用户不应该看到。

同时修正文档中“三种模式”和“模式 D 四种组合”之间的认知冲突。

建议最终产品模型：

```text
模拟
  └─ 可以选择测试网行情 / 主网行情

测试
  └─ Binance Testnet 真执行

实盘
  └─ Binance Mainnet 真执行
```

底层组合继续由程序自动推导。

## P0 “一键进入无人值守”

新增一个明确的产品流程：

```text
首次启动
   ↓
系统自检
   ↓
配置向导
   ↓
确认 API
   ↓
确认模式
   ↓
确认风险参数
   ↓
系统自动验证
   ↓
READY
   ↓
开始无人值守
```

完成之后页面应该明确告诉用户：

```text
系统已进入无人值守运行

当前模式：实盘
交易标的：SOLUSDT
账户权益：XXXX
当前风险等级：LOW
交易许可：BUY / SELL
策略版本：Vxx
最近对账：正常
系统健康：正常

无需人工操作。
```

不要再让用户自己理解 readiness。

## P0 建立“运行结论卡”

首页第一屏不要首先展示技术指标。

第一屏应该直接回答：

```text
系统现在怎么样？
```

建议：

```text
┌──────────────────────────────────────┐
│ 系统运行正常                         │
│                                      │
│ ● 实盘运行                           │
│ ● 无人值守                           │
│ ● 当前允许交易                       │
│                                      │
│ Binance       正常                   │
│ 行情           正常                   │
│ 对账           正常                   │
│ 风控           正常                   │
│ 策略           正常                   │
│                                      │
│ 当前持仓       XX SOL                │
│ 当前权益       ¥XXXX                 │
│ 今日收益       +X.XX%                │
│ 今日交易       X 次                  │
│                                      │
│ 下一次系统动作：自动                 │
└──────────────────────────────────────┘
```

最重要的是：

> **用户打开页面后 5 秒内知道系统是否正常。**

## P0 所有状态必须“人话化”

不要只输出：

```text
SAFE_MODE
RECOVERY
DEGRADED
KILLED
RECONCILIATION_FAILED
EQUITY_DRIFT
```

应该同时输出：

```text
系统正在恢复

原因：
交易所账户数据暂时无法确认。

系统动作：
正在自动重新连接并进行账户对账。

当前交易：
BUY 已暂停
SELL 已暂停

用户操作：
无需操作

预计下一步：
系统将在 30 秒后自动重试。
```

如果真的需要人工：

```text
需要你的确认

原因：
检测到账户资产与系统账本存在无法自动解释的差异。

系统已经：
✓ 停止交易
✓ 完成 3 次自动对账
✓ 保留现场证据

你需要：
确认 Binance 当前账户资产是否正确。

[查看差异]
[确认账户状态]
```

## P0 急停重新定义

急停不是日常运维按钮。

系统必须支持：

```text
自动急停
```

例如：

```text
严重权益异常
严重对账异常
数据源失真
关键任务连续失败
未知订单状态
账户状态无法确认
```

自动：

```text
KILL
↓
冻结交易
↓
记录原因
↓
保存 evidence
↓
进入 recovery loop
```

用户不需要立即操作。

### 人工急停

保留一个非常明显的：

```text
立即停止交易
```

点击后立即生效。

不要二次确认。

当前设计已经遵循这一点，继续保持。架构文档也明确急停属于冻结方向，应即时可用。

### 自动恢复

不要让普通恢复变成：

```text
用户 → recover → restart → check → reset → restart
```

而应该：

```text
KILL
 ↓
自动进入 RECOVERY_CHECK
 ↓
重新连接
 ↓
重新获取账户
 ↓
重新对账
 ↓
重新检查行情
 ↓
重新检查风控
 ↓
全部正常
 ↓
自动恢复
```

但是：

> 对“人工主动 KILL”与“重大资金异常 KILL”保留人工恢复确认。

这样兼顾无人值守与资金安全。

## P0 用户通知模型

系统状态分成：

```text
NORMAL
NOTICE
DEGRADED
ACTION_REQUIRED
KILLED
```

其中：

### NORMAL

不打扰用户。

### NOTICE

记录并展示：

```text
今日发生 3 次 WS 重连，均已自动恢复。
```

### DEGRADED

系统继续运行，但降低能力：

```text
行情延迟升高
BUY 暂停
SELL 保留
系统正在自动恢复
```

### ACTION_REQUIRED

真正需要人：

```text
账户 API 权限发生变化
API Key 无效
资金差异无法自动解释
策略版本需要确认
```

### KILLED

明确：

```text
交易已停止
原因
时间
系统已经做了什么
当前资金状态
是否需要人工处理
```

## P0 日志产品化

日志不能只服务开发人员。

保留技术日志，同时新增：

```text
operator event
```

例如：

```text
14:32:01 系统启动
14:32:03 Binance 连接成功
14:32:05 账户同步完成
14:32:07 对账完成
14:32:10 策略进入 READY
14:35:21 发现 BUY 信号
14:35:21 风控通过
14:35:22 BUY 订单提交
14:35:23 成交
14:35:23 持仓更新
14:35:24 交易完成
```

用户看到的是：

> 发生了什么

开发人员看到的是：

> 为什么发生

两者必须共存。

## P0 交易结果必须可解释

每次交易产生一条“人话交易结论”。

例如：

```text
BUY SOLUSDT

结果：成功
价格：$XXX
数量：X SOL
金额：$XXX

为什么买：
趋势：上升
资金流：正向
市场状态：TRENDING
策略评分：86

风控：
单笔风险：正常
日亏损：正常
账户敞口：正常

执行：
订单：FILLED
滑点：0.XX%
耗时：XXXms
```

SELL 同理。

这样未来 AI 可以直接读取。

## P0 AI 复盘数据产品化

当前系统已经有：

* trade_records
* strategy performance
* daily report
* HODL benchmark
* signals
* risk events
* execution events
* evidence chain
* optimizer proposal

这些不要继续各自孤立。

增加统一的：

```text
AI Review Package
```

每个交易周期 / 每日自动生成：

```text
review/
  YYYY-MM-DD/
    summary.json
    trades.json
    signals.json
    risk.json
    execution.json
    market_regime.json
    performance.json
    anomalies.json
    strategy_version.json
    ai_review.md
```

AI 输入应该能回答：

```text
今天赚了还是亏了？
为什么？
哪些信号有效？
哪些信号误判？
哪个市场环境表现差？
执行有没有问题？
风控有没有误杀？
策略是否过度交易？
与 HODL 比怎么样？
哪些参数值得研究？
```

## P0 AI 只能分析，不直接交易

继续保持当前红线：

```text
AI
 ↓
Review
 ↓
Analysis
 ↓
Proposal
 ↓
Human approval
 ↓
Strategy Version
```

禁止：

```text
AI → BUY
AI → SELL
```

## P0 建立“系统每日自我复盘”

每天自动生成：

```text
今日系统复盘

运行时间：23h 58m
交易次数：8
胜率：62.5%
收益：+1.21%
最大回撤：0.73%
HODL：+0.41%

系统稳定性：
WS 重连：2
API timeout：1
自动恢复：3
人工干预：0

策略：
趋势策略：+1.42%
均值回归：-0.21%

问题：
1. 14:32 一次流动性异常
2. 两次 SELL 滑点偏高

结论：
今天系统运行正常。

建议 AI 进一步研究：
- SELL 滑点与 market regime 的关系
- 均值回归策略在当前 regime 下的表现
```

## P1 “用户无需看日志”

首页提供：

```text
今天发生了什么？
```

而不是要求用户：

```text
docker compose logs
grep
tail
curl
```

技术日志仍然存在，但属于：

```text
高级诊断
```

## P1 自动恢复策略

检查现有：

* runtime_supervisor
* reconnect
* reconciliation
* recovery
* SAFE_MODE
* KILLED
* task restart

统一成明确的恢复策略：

```text
Transient Error
→ retry

Repeated Error
→ degraded

Critical Error
→ pause

Unsafe State
→ kill

Recoverable
→ automatic recovery

Unknown Financial State
→ stay frozen + human confirmation
```

原则：

> **系统宁可自己停，也不要要求用户不断看守。**

## P1 配置体验

用户第一次配置只需要：

```text
Binance API Key
Binance API Secret

运行模式
○ 模拟
○ 测试
○ 实盘

风险配置
[使用推荐默认值]

交易标的
SOLUSDT

确认：
☑ 我了解实盘使用真实资金
```

默认值由系统提供。

不要让用户填写几十个参数。

高级参数进入：

```text
高级配置
```

并明确：

```text
修改这些参数通常不需要人工参与日常运行。
```

## P1 配置变更

普通参数：

```text
修改
→ 系统验证
→ 保存
→ 自动安排重启
→ 自动验证
→ 恢复运行
```

用户不需要：

```text
保存
→ 自己 docker restart
→ 自己 curl
→ 自己确认
```

页面只告诉：

```text
配置已更新

系统正在重新加载……

✓ 配置验证
✓ 服务重启
✓ 数据库正常
✓ 风控正常
✓ 对账正常

系统已恢复无人值守。
```

注意：

真正的高风险配置仍然需要人工确认。

## P1 部署 UX

Docker / Pi 部署不应该让用户面对：

```text
docker build
docker compose
health
migration
schema
volume
permission
```

这些全部自动化。

最终目标：

```text
配置 production.env
↓
docker compose up -d
↓
系统自动完成剩余工作
```

然后页面显示：

```text
部署完成

版本：V13.x
数据库：正常
Binance：正常
行情：正常
风控：正常
对账：正常

系统状态：
READY

无人值守：ON
```

## P1 `/ops` 产品定位改变

`/ops` 不再是：

> “用户需要逐项检查的部署考试页面”

而是：

> “系统给用户看的健康报告”。

例如：

```text
系统健康

✓ 配置
✓ 数据库
✓ Binance
✓ 行情
✓ 风控
✓ 对账
✓ 策略
✓ 执行
✓ 磁盘
✓ 日志

结论：

系统可以无人值守运行。
```

只有异常项目展开技术细节。

## P1 自动生成 AI Evidence Package

新增一个只读接口：

```text
GET /api/ai-review/latest
```

返回统一结构：

```json
{
  "period": "...",
  "system_status": "...",
  "performance": {},
  "trades": [],
  "signals": [],
  "risk_events": [],
  "execution_events": [],
  "reconciliation": {},
  "market_regimes": [],
  "anomalies": [],
  "strategy_version": {},
  "recommendation_candidates": []
}
```

敏感信息必须过滤：

* API key
* secret
* token
* credentials

不得进入 AI Review Package。

## P1 用户可读报告

同时提供：

```text
/api/reports/daily
/api/reports/trades
/api/reports/system
/api/ai-review/latest
```

并允许 Web 页面查看。

## P1 产品首页最终结构

建议：

```text
首页

[系统运行正常]
[无人值守运行中]

模式：实盘
交易：允许
账户：¥XX,XXX
今日收益：+X.XX%

────

系统状态

Binance       ✓
行情           ✓
风控           ✓
对账           ✓
策略           ✓
执行           ✓

────

今日交易

BUY ...
SELL ...
BUY ...

────

今日系统复盘

收益
胜率
最大回撤
HODL 对比
异常
自动恢复次数

────

最近需要你关注的事情

无

────

[立即停止交易]
```

如果没有问题：

> **不要显示一堆“绿色 PASS”。**

直接显示：

> **系统正常，无需操作。**

## P1 产品化原则

所有页面遵循：

```text
结论 > 原因 > 影响 > 系统动作 > 用户动作
```

而不是：

```text
技术字段 > 状态码 > 日志 > 用户自己判断
```

例如：

错误：

```text
equity_drift = 0.032
reconcile_killed = true
```

正确：

```text
系统已暂停交易

原因：
账户资产与系统预期存在 3.2% 差异。

系统动作：
✓ 已暂停 BUY
✓ 已暂停 SELL
✓ 正在重新同步 Binance
✓ 已完成 2 次自动对账

用户动作：
暂时无需操作
```

## P1 测试要求

新增产品级测试：

### 无人值守测试

验证：

```text
异常
→ 自动恢复
→ 无人工操作
→ 最终 READY
```

### 用户状态测试

验证：

```text
每一种内部状态
都有：
人话标题
原因
影响
系统动作
用户动作
```

### 配置测试

验证：

```text
正常配置
→ 一次配置
→ 自动启动
→ READY
```

### 高风险操作测试

验证：

```text
live
risk change
strategy activate
重大 recovery
```

仍然需要人工确认。

## P1 不得引入的复杂度

禁止：

* 微服务
* Kubernetes
* Redis 强依赖
* 第二交易所
* 第二币种
* 高频
* AI 自动下单
* 复杂权限系统
* 企业级 SSO
* 多租户
* 云端控制平面

当前：

```text
Python
asyncio
single process
SQLite
Docker
Raspberry Pi
LAN
```

继续保持。

## P2 Evidence Chain 收口

在产品 UX 完成后继续完成现有工程缺口：

```text
BUY
SELL
duplicate signal
clientOrderId
UNKNOWN timeout
recovery
kill switch
reconciliation drift
restart recovery
```

必须能形成完整：

```text
signal
 ↓
intent
 ↓
order
 ↓
execution_event
 ↓
fill
 ↓
trade
 ↓
accounting
 ↓
risk_event
 ↓
reconciliation
 ↓
daily review
 ↓
AI review package
```

并且每个交易周期都能通过唯一关联 ID 找回完整证据链。

## P2 Docker Compose

完成当前已有但尚未 EXECUTED 的：

```text
docker compose build
docker compose up
health
operator-status
mode
config
apply
restart
DB persistence
down
up
再次验证
```

必须真实执行。

不要只根据 CI 或代码推断 PASSED。

## P2 Testnet

如果当前环境已有必要 key：

执行真实 Testnet：

```text
startup
account sync
market data
signal
risk
order
fill
accounting
reconciliation
restart
```

如果没有 key：

```text
BLOCKED
```

不要写：

```text
PASSED
```

## P2 Pi

只有：

```text
LOCAL = PASSED
DOCKER = PASSED
EVIDENCE_CHAIN = PASSED
TESTNET = PASSED 或明确 BLOCKED
```

后才进入 Pi。

Pi 继续保持：

```text
ARM64
Docker
LAN
无人值守
```

## 文档要求

本轮必须同步维护：

```text
README.md
CLAUDE.md
docs/progress.md
docs/README.md
docs/architecture.md
docs/operating-modes-manual.md
docs/runbook.md
docs/verification/local-verification.md
docs/verification/docker-verification.md
docs/verification/pi-deployment.md
```

新增建议：

```text
docs/product/operator-experience.md
docs/product/unattended-operation.md
docs/ai-review-spec.md
```

其中明确：

```text
什么事情机器做
什么事情机器自动恢复
什么事情需要人
什么事情绝对不需要人
```

## 重要：修正文档事实漂移

必须实际检查并修正文档中的：

* 三模式 / 四组合概念冲突
* `/admin` 仍描述“写配置文件”的旧语义
* `/ops` 仍以部署检查为主要用户职责的描述
* Pi 状态描述
* 当前 HEAD
* 当前测试数量
* 当前 Level 1 / Level 2 / Level 3 状态

所有状态严格使用：

```text
IMPLEMENTED
READY_TO_RUN
EXECUTED
PASSED
FAILED
BLOCKED
NOT_EXECUTED
```

禁止用“应该可以”“理论上可以”冒充 PASSED。

## Git 要求

完成每个独立工作单元后：

```text
git status
git diff
检查 secrets
ruff
pytest -m "not testnet"
```

然后：

```text
commit
push origin/main
```

只使用 `main`。

禁止 rewrite history。

## 最终验收报告

完成后必须输出：

```text
V13 Productization Result

产品目标：
无人值守 / 低摩擦

用户首次配置步骤：
X 步

日常人工操作：
X 次

普通异常人工干预：
0

自动恢复：
YES/NO

急停：
自动 + 人工

AI Review：
READY / NOT_READY

LOCAL：
PASSED/FAILED

DOCKER：
PASSED/FAILED

EVIDENCE_CHAIN：
PASSED/FAILED

TESTNET：
PASSED/BLOCKED/NOT_EXECUTED

PI：
PASSED/BLOCKED/NOT_EXECUTED

最终状态：
READY_FOR_PI / NOT_READY_FOR_PI / READY_FOR_SMALL_CAPITAL

HEAD：
<实际 SHA>

工作区：
CLEAN/DIRTY

push：
成功/失败
```

## 最重要的验收标准

最终不要以：

```text
测试数量
代码行数
配置项数量
页面数量
```

作为本轮主要成功标准。

真正的成功标准是：

> 一个第一次接触系统的人，在配置必要 Key 和做出必要资金/模式确认之后，可以让系统自己运行。

以及：

> 系统运行过程中出现普通故障时，不需要人类值守。

以及：

> 用户打开页面时，不需要阅读日志，就知道系统现在是否安全、是否交易、发生了什么、是否需要自己做什么。

如果用户不需要做任何事情：

> **明确告诉用户“无需操作”。**

最后继续保持：

```text
AI 可以分析
AI 可以复盘
AI 可以提出优化建议
AI 不可以直接下单
```

完成后更新全部相关文档、保存 `docs/progress.md` 进度，并 commit + push `origin/main`。
