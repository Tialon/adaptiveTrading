# adaptiveTrading V15：安全模式 / 急停恢复 / 全流程故障注入验收任务书

## 1. 任务目标

当前项目已经完成：

* Windows SQLite 功能矩阵
* Windows Docker + MySQL + Redis
* Docker 生命周期验证
* Redis 掉线/恢复
* MySQL 不可达处理
* 1 小时稳定性验证
* 急停/恢复基础链路
* 操作者状态人话化
* 自动恢复边界
* 配置持久化
* 三模式管理

但是当前真实用户验收暴露出一个必须彻底收口的问题：

```text
系统进入安全模式
↓
equity = KILL
position = PAUSE
cash = PAUSE
↓
行情静默
↓
资金漂移
↓
用户点击「恢复急停」
↓
仍然无法恢复到可交易状态
```

本阶段不要新增交易策略、币种、合约、AI 下单、高频交易等功能。

唯一目标：

> **把“异常 → 安全冻结 → 人工恢复 → 条件重新验证 → 正常运行”这条链做成真正可验证、可解释、可重复的完整闭环。**

---

## 2. 第一原则：禁止为了通过测试而降低安全门槛

本任务最重要的约束：

**禁止通过以下方式解决问题：**

```text
降低 equity KILL 阈值
取消资金漂移检查
忽略 position mismatch
忽略 cash mismatch
忽略行情静默
恢复急停后直接允许交易
关闭 TradingGate
关闭 reconciliation
把 KILL 强制改成 NORMAL
恢复按钮直接清除所有风险状态
```

绝对不能出现：

```python
recover()
→ force_normal()
→ can_buy = True
```

正确逻辑必须始终是：

```text
Recover
    ↓
解除人工冻结
    ↓
重新执行安全条件检查
    ↓
TradingGate
    ↓
条件全部满足
    ↓
允许交易
```

即：

> **解冻 ≠ 允许交易**

这一条必须继续作为核心安全契约。

---

## 3. 第一阶段：完整定位当前“恢复不了”的真实原因

先不要修改代码。

必须先复现用户当前看到的问题。

执行：

```text
启动系统
↓
记录启动快照
↓
制造/等待行情静默
↓
制造/等待资金漂移
↓
进入 KILL
↓
记录完整状态
↓
点击恢复急停
↓
记录恢复后的每一个状态变化
```

必须把以下状态逐项记录：

```text
trading_mode
lifecycle
risk_state
emergency_stop
breaker
equity
position
cash
market_data
reconciliation
critical_tasks
TradingGate
can_buy
can_sell
```

不要只记录最终：

```text
can_buy=False
```

必须找到：

> **到底是哪一个条件在阻止恢复。**

---

## 4. 建立“恢复状态机”完整证据

明确记录：

```text
NORMAL
  ↓
异常
  ↓
PAUSED / KILLED
  ↓
SAFE_MODE
  ↓
ACTION_REQUIRED
  ↓
人工恢复
  ↓
RECOVERY_CHECK
  ↓
重新检查所有安全条件
  ↓
NORMAL / TRADING
```

如果恢复后仍然：

```text
KILL
```

必须明确说明：

```text
是谁保持 KILL
为什么保持 KILL
哪个条件没有恢复
这个条件能否由系统自证
是否需要人工处理
```

禁止出现：

```text
恢复失败
```

这种没有原因的结论。

---

## 5. 第二阶段：针对当前三个真实故障建立故障注入测试

必须建立可重复的 fault injection，不允许依赖偶然网络环境。

### 5.1 行情静默

制造：

```text
last_market_data_at
超过静默阈值
```

验证：

```text
行情正常
↓
行情静默
↓
系统发现静默
↓
禁止交易
↓
进入正确风险等级
```

然后：

```text
恢复行情
↓
收到新的可信 tick
↓
系统自动确认行情恢复
↓
重新计算 TradingGate
```

验证：

```text
行情恢复 ≠ 直接解除 KILL
```

必须经过完整安全检查。

---

## 6. 资金漂移故障注入

模拟：

```text
本地 equity ≠ exchange equity
```

至少测试：

```text
equity drift
position drift
cash drift
```

例如：

```text
本地：
equity = 10000

交易所：
equity = 9000
```

验证：

```text
drift detected
↓
equity = KILL
↓
position/cash 根据规则进入正确等级
↓
禁止交易
```

然后恢复：

```text
exchange account = expected state
```

再次执行：

```text
reconciliation
↓
确认连续通过
↓
重新计算风险
↓
TradingGate
```

必须确认：

> 资金漂移恢复后，系统不会因为旧的 KILL 状态永久卡死。

---

## 7. 账户 / 持仓 mismatch

必须单独测试：

```text
本地：
SOL = 10

交易所：
SOL = 8
```

进入：

```text
position mismatch
```

验证：

```text
KILL / PAUSE
↓
安全模式
↓
人工恢复
```

此时必须明确：

```text
系统不能自己认为已经恢复
```

因为：

> 真实账户与本地账本不一致时，系统不能拿自己的错误账本证明自己正确。

因此正确结果可以是：

```text
ACTION_REQUIRED

需要用户确认：
Binance 账户资产 / 持仓
与系统账本是否一致。
```

这个行为不能被改成自动恢复。

---

## 8. “恢复急停”必须重新定义为 Recovery Check

检查现有：

```text
POST /api/emergency/recover
```

要求：

它不是：

```text
清除 KILL
```

而应该是：

```text
请求系统开始恢复检查
```

推荐语义：

```text
用户点击恢复急停
        ↓
解除 MANUAL emergency stop
        ↓
Recovery Check
        ↓
检查行情
检查账户
检查资金
检查持仓
检查对账
检查关键任务
检查配置
检查 TradingGate
        ↓
条件满足
        ↓
恢复 TRADING
```

条件不满足：

```text
保持 PAUSED / KILLED
```

并返回：

```text
恢复失败
原因：
资金漂移仍存在

影响：
暂时无法交易

系统动作：
继续保持安全冻结

用户动作：
请核对 Binance 账户资产
```

---

## 9. 必须区分三类恢复

建立明确测试：

### A. 人工急停

```text
MANUAL
```

不能自动恢复。

必须：

```text
用户恢复
↓
Recovery Check
```

### B. 系统可以自证的故障

例如：

```text
AUTO_DATA
AUTO_TASK
```

允许自动恢复，但必须有真实恢复证据。

例如：

```text
行情恢复
↓
新 tick
↓
时间戳正常
↓
数据校验通过
```

才允许进入下一阶段。

### C. 无法自证的资金异常

例如：

```text
AUTO_EQUITY
AUTO_ACCOUNTING
AUTO_RECONCILE
```

原则：

```text
冻结
↓
不要自行解除
↓
ACTION_REQUIRED
↓
人工确认
```

这个边界不要修改。

---

## 10. 第三阶段：建立“恢复后是否真正可交易”的测试

这是本阶段最关键的一组测试。

不能只验证：

```text
risk_state = NORMAL
```

必须验证：

```text
recover
↓
risk
↓
lifecycle
↓
reconciliation
↓
TradingGate
↓
can_buy
↓
can_sell
```

至少测试以下组合：

```text
恢复成功 + 全部正常
恢复成功 + 行情未恢复
恢复成功 + equity drift
恢复成功 + position mismatch
恢复成功 + cash mismatch
恢复成功 + reconciliation failure
恢复成功 + critical task down
恢复成功 + Redis degraded
恢复成功 + MySQL unavailable
```

最终必须保证：

```text
任何安全条件没有恢复
        ↓
不能交易
```

---

## 11. 第四阶段：BUY / SELL 完整 Evidence Chain

当前：

```text
EVIDENCE_CHAIN = NOT_EXECUTED
```

必须真正收口。

建立：

```text
Signal
↓
Risk Decision
↓
Trading Intent
↓
Order
↓
Execution Event
↓
Fill
↓
Accounting
↓
Trade Record
↓
Risk Event
↓
Reconciliation
↓
AI Review
```

每一步必须能够通过：

```text
trace_id
correlation_id
order_id
client_order_id
```

关联。

---

## 12. Evidence Chain 至少测试以下场景

### BUY

```text
Signal
→ BUY
→ Risk Pass
→ Order
→ Fill
→ Accounting
→ Trade
→ Reconciliation
```

### SELL

```text
Signal
→ SELL
→ Risk Pass
→ Order
→ Fill
→ Accounting
→ Trade
→ Reconciliation
```

### 重复 Signal

```text
same signal
× 2
```

必须：

```text
最多产生一个有效交易意图
```

### 重复 clientOrderId

必须验证：

```text
不会产生重复订单
```

### Order timeout

模拟：

```text
request sent
↓
response timeout
```

必须：

```text
query exchange
↓
确定订单状态
```

不能直接重新 BUY。

### UNKNOWN

必须验证：

```text
UNKNOWN
↓
禁止重复下单
↓
进入 recovery / reconciliation
```

### Restart

```text
Order pending
↓
restart
↓
恢复
↓
重新查询 exchange
```

必须能够继续处理。

---

## 13. 第五阶段：恢复按钮 UI 验收

实际浏览器测试：

```text
/
 /setup
 /admin
 /ops
```

重点测试当前用户反馈的场景：

```text
安全模式
```

页面必须明确：

```text
系统处于安全模式

原因
资金熔断：KILL
行情静默：31 秒
资金漂移：KILL

影响
当前无法交易

系统动作
已冻结交易

用户动作
请确认 Binance 账户资产与系统账本一致
```

点击：

```text
恢复急停
```

必须立即告诉用户：

### 如果恢复成功

```text
恢复检查通过

系统状态：正常
交易能力：已恢复

无需继续操作
```

### 如果恢复失败

```text
恢复检查未通过

原因：
资金漂移仍存在

影响：
系统继续保持安全冻结

系统动作：
继续阻止交易

用户动作：
请核对 Binance 账户资产
```

绝对不能：

```text
按钮显示“恢复成功”
但 3 秒后又 KILL
```

如果条件没有恢复，第一次响应就应该如实说明。

---

## 14. 第六阶段：异常恢复矩阵

建立自动化测试矩阵：

| 故障                     |    是否 KILL |                是否自动恢复 |  是否需要人工 |
| ---------------------- | ---------: | --------------------: | ------: |
| 人工急停                   |        YES |                    NO |     YES |
| 行情短暂静默                 |      按现有规则 |                   YES |      NO |
| 行情长期异常                 |  YES/PAUSE |                 视自证结果 |     必要时 |
| Critical Task 崩溃       | PAUSE/KILL |                   YES | 超过重启额度后 |
| Redis 掉线               |       当前规则 |                   YES |      NO |
| MySQL 掉线               |        YES |               恢复后重新检查 |     必要时 |
| Equity Drift           |       KILL |                    NO |     YES |
| Position Mismatch      |      按现有规则 |                    NO |     YES |
| Cash Mismatch          |      按现有规则 |                    NO |     YES |
| Accounting Error       |       KILL |                    NO |     YES |
| UNKNOWN Order          | KILL/PAUSE |                    NO |     YES |
| Reconciliation Failure |      按现有规则 | 连续失败后 ACTION_REQUIRED |     YES |

不要擅自改变既有风险等级。

如果实际代码与表格不一致：

> 以真实代码行为为准，然后更新文档。

---

## 15. 第七阶段：Windows SQLite 完整回归

执行：

```powershell
.\scripts\start-local.ps1
```

确认：

```text
SQLite
Redis OFF
MySQL OFF
```

然后运行：

```text
functional_matrix
pytest
ruff
mypy
```

重点新增：

```text
安全模式
行情静默
资金漂移
position mismatch
cash mismatch
人工急停
恢复急停
恢复失败
恢复成功
```

---

## 16. 第八阶段：Windows Docker 完整回归

环境必须保持：

```text
Windows
Docker Desktop

adaptive-trading
MySQL 8
Redis 7
```

执行：

```text
docker compose build
docker compose up -d
```

然后验证：

```text
MySQL healthy
Redis healthy
Application healthy
```

继续测试：

```text
application restart
MySQL restart
Redis restart
docker compose down
docker compose up -d
```

确保：

```text
配置不丢
数据库不丢
事件不丢
状态恢复
服务恢复
```

---

## 17. 第九阶段：真实运行稳定性

重新执行稳定性测试。

最低：

```text
1 小时
```

必须继续记录：

```text
应用不可达
容器 restart
MySQL restart
Redis restart
critical task failure
UNKNOWN order
recovery_required
reconciliation
TradingGate
can_buy
can_sell
CPU
memory
DB connection
Redis connection
WebSocket
market data
operator event
```

不能只检查：

```text
health = 200
```

---

## 18. 第十阶段：用户当前问题必须形成回归测试

用户当前真实问题：

```text
系统进入安全模式
↓
资金 KILL
↓
行情静默
↓
资金漂移
↓
点击恢复急停
↓
无法恢复
```

必须最终成为一个自动化 regression test。

测试名称建议：

```text
test_emergency_recovery_after_market_silence_and_equity_drift
```

测试必须明确验证：

```text
异常发生
→ KILL
→ 用户恢复
→ 恢复检查
→ 条件未恢复时继续冻结
→ 条件恢复
→ 再次执行安全检查
→ TradingGate
→ 最终恢复
```

不能只验证最终状态。

---

## 19. 最终验收标准

最终 `docs/progress.md` 必须增加：

```text
SAFETY_MODE_ENTRY                 = PASSED / FAILED
MARKET_SILENCE_RECOVERY           = PASSED / FAILED
EQUITY_DRIFT_RECOVERY             = PASSED / FAILED
POSITION_MISMATCH_RECOVERY        = PASSED / FAILED
CASH_MISMATCH_RECOVERY            = PASSED / FAILED

EMERGENCY_RECOVER_SUCCESS         = PASSED / FAILED
EMERGENCY_RECOVER_BLOCKED         = PASSED / FAILED
RECOVERY_TRADING_GATE              = PASSED / FAILED

BUY_EVIDENCE_CHAIN                = PASSED / FAILED
SELL_EVIDENCE_CHAIN               = PASSED / FAILED
DUPLICATE_SIGNAL                  = PASSED / FAILED
DUPLICATE_CLIENT_ORDER_ID         = PASSED / FAILED
ORDER_TIMEOUT                     = PASSED / FAILED
UNKNOWN_ORDER                     = PASSED / FAILED
RESTART_RECOVERY                  = PASSED / FAILED
RECONCILIATION                    = PASSED / FAILED

WINDOWS_SQLITE_REGRESSION         = PASSED / FAILED
WINDOWS_DOCKER_REGRESSION         = PASSED / FAILED
STABILITY_1H                      = PASSED / FAILED
USER_CURRENT_ISSUE_REGRESSION     = PASSED / FAILED

EVIDENCE_CHAIN                    = PASSED / FAILED / NOT_EXECUTED
WINDOWS_PRE_PRODUCTION             = YES / NO
READY_FOR_PI                       = YES / NO
```

---

## 20. 文档维护

完成后必须同步检查并维护：

```text
README.md
CLAUDE.md
docs/progress.md
docs/architecture.md
docs/runbook.md
docs/verification/local-verification.md
docs/verification/docker-verification.md
docs/product/operator-experience.md
docs/product/unattended-operation.md
docs/ai-review-spec.md
docs/verification/pi-deployment.md
```

重点记录：

```text
恢复急停 ≠ 强制解除风险
恢复急停 = 启动恢复检查
```

以及：

```text
哪些故障系统可以自愈
哪些故障必须人工确认
为什么必须人工确认
```

同时删除任何已经与实际代码不一致的旧文案。

---

## 21. 证据要求

所有结果严格使用：

```text
IMPLEMENTED
READY_TO_RUN
EXECUTED
PASSED
FAILED
BLOCKED
NOT_EXECUTED
```

禁止使用：

```text
应该可以
理论可行
基本完成
看起来没问题
生产可用
应该稳定
```

没有真实执行，就必须：

```text
NOT_EXECUTED
```

---

## 22. 代码质量门禁

每个工作单元完成后执行：

```powershell
git diff
git diff --check

ruff check .
mypy .

pytest -m "not testnet"
```

如果新增测试：

```text
必须执行新增测试
必须记录实际结果
```

如果涉及 Docker：

```text
必须实际执行 Docker
不能只看 YAML
```

如果涉及恢复：

```text
必须实际执行故障注入
不能只看 unit test
```

---

## 23. 安全边界

本阶段：

```text
禁止主网真钱交易
禁止扩大交易权限
禁止关闭安全门
禁止删除 reconciliation
禁止绕过 TradingGate
禁止 AI 直接下单
禁止增加策略
禁止增加币种
禁止增加 futures
禁止高频交易
```

Testnet 可以用于真实交易链路验证。

Mainnet 保持关闭。

---

## 24. 最终用户验收

当：

```text
Windows SQLite regression = PASSED
Windows Docker regression = PASSED
Safety recovery = PASSED
Evidence Chain = PASSED
Stability = PASSED
```

CC 在 Windows Docker 环境保持服务运行。

输出：

```text
Windows 准生产服务已启动

访问地址：
http://localhost:8800
```

然后停止继续自动修改系统。

等待用户浏览器验收。

用户只需要：

```text
打开浏览器
查看首页
查看 /setup
查看 /admin
查看 /ops
测试急停
测试恢复
```

不要让用户执行：

```text
curl
docker exec
mysql
redis-cli
```

---

## 25. 本阶段完成后的判断

只有满足：

```text
EVIDENCE_CHAIN = PASSED
SAFETY_RECOVERY = PASSED
STABILITY = PASSED
USER_UI_ACCEPTANCE = PASSED
```

才允许：

```text
READY_FOR_PI = YES
```

否则：

```text
READY_FOR_PI = NO
```

不要提前进入 Pi。

---

## 26. 进度保存与 Git

本任务必须持续维护项目进度。

每完成一个工作单元：

```text
更新 docs/progress.md
```

同步维护相关：

```text
README.md
CLAUDE.md
docs/architecture.md
docs/runbook.md
docs/verification/*
docs/product/*
```

确保文档描述与真实代码、真实测试结果一致。

每个阶段完成后：

```powershell
git diff
git diff --check
```

确认没有：

```text
.env
API Key
Secret
数据库
日志
运行时 evidence
临时文件
```

被错误提交。

最终：

```powershell
git status
git log -1
```

确认工作区状态。

然后：

```text
git add
git commit
git push origin main
```

提交信息必须准确描述实际完成内容。

最终报告必须包含：

```text
本次完成内容
实际执行的测试
PASSED
FAILED
BLOCKED
NOT_EXECUTED

当前：
EVIDENCE_CHAIN = ?
READY_FOR_PI = ?

最终 commit SHA
push result
```

**只有 GitHub 实际确认 push 成功后，才能写“已推送远程仓库”。**

---

## 27. 最终要求

不要把本阶段理解成：

> “把恢复按钮修好。”

真正目标是：

> **把整个交易系统的安全生命周期跑通一次。**

最终应该能够证明：

```text
正常运行
   ↓
异常
   ↓
系统发现
   ↓
自动冻结
   ↓
用户看得懂
   ↓
系统说明原因
   ↓
可自愈故障自动恢复
   ↓
不可自证故障要求人工
   ↓
人工恢复
   ↓
重新执行完整安全检查
   ↓
TradingGate
   ↓
恢复交易能力
```

并且每一步都有：

```text
代码
测试
运行证据
日志
数据库记录
UI 展示
文档
```

**不要追求“看起来能恢复”，要证明“真实运行后能够安全恢复”。**
