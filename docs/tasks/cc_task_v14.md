# adaptiveTrading V14 下一阶段任务书

## 1. 任务目标

当前项目已经进入：

> **功能开发收口 → Windows 本机功能验证 → Windows Docker 准生产 → 用户功能验收 → Pi Docker 生产**

本阶段不要新增交易策略、币种、合约、AI 下单、高频交易等功能。

重点只有三个：

1. **Windows SQLite：完成 CC 本机功能验证**
2. **Windows Docker：正式切换为 MySQL + Redis 明确依赖，并部署启动服务**
3. **把 Windows Docker 服务交给用户进行功能验收**

后续：

```text
Windows SQLite
    ↓
CC 功能验证
    ↓
Windows Docker
    ↓
MySQL + Redis
    ↓
稳定运行验证
    ↓
用户浏览器功能验收
    ↓
READY_FOR_PI
    ↓
Pi Docker + MySQL + Redis
    ↓
长时间无人值守
```

本任务完成后，不代表进入真钱实盘。

---

## 2. 当前技术栈冻结

正式确认以下环境模型，不再反复讨论：

### Level 1：Windows 测试环境

```text
Windows
├── Python 3.13
├── SQLite
├── Redis：不需要
├── MySQL：不需要
└── python run.py
```

目的：

* 单元测试
* 集成测试
* 页面功能测试
* API 测试
* 模式切换
* 配置持久化
* 风控/恢复
* UI 功能验证

这里追求的是：

> **开发方便、启动简单、CC 能快速验证。**

不要为了 Level 1 强行安装 MySQL / Redis。

现有文档已经存在 SQLite 启动路径，但仍需要进一步把它做成真正的一键开发路径，而不是要求人工临时覆盖多个环境变量。

---

## 3. Level 2：Windows Docker 准生产环境

正式要求：

```text
Windows 11
└── Docker Desktop
    ├── adaptive-trading
    ├── MySQL 8
    └── Redis 7
```

正式运行关系：

```text
adaptive-trading
       │
       ├── MySQL 8
       │
       └── Redis 7
```

这里开始：

> **MySQL 和 Redis 都属于明确依赖，不允许再把它们当成可选依赖。**

必须做到：

```text
MySQL 不可用
    ↓
应用不能进入正常运行状态

Redis 不可用
    ↓
按照代码实际使用情况决定：
如果 Redis 属于运行时关键依赖 → 应用阻止进入正常运行
如果只是非关键缓存 → 必须明确记录为降级状态
```

不要凭文档判断 Redis 是否关键。

请实际检查：

```text
Redis 在哪些模块被调用
是否用于：
- 分布式锁
- 幂等
- 状态
- 缓存
- 任务协调
- WebSocket
- 风控
- TradingGate
- Execution
```

根据真实代码确定依赖等级。

最终文档必须明确：

```text
MySQL = REQUIRED
Redis  = REQUIRED / OPTIONAL
```

如果 Redis 实际上已经属于生产安全链路，则本阶段直接改为：

```text
Redis = REQUIRED
```

---

## 4. 第一阶段：Windows SQLite 本机功能验证

CC 在 Windows 本机首先完成完整功能测试。

不要先部署 Docker。

### 4.1 一键启动

优先实现：

```powershell
.\scripts\start-local.ps1
```

或者同等简单方式。

目标：

```text
SQLite
Redis OFF
MySQL OFF
python run.py
```

直接打开：

```text
http://127.0.0.1:8800
```

不要要求用户手工执行：

```powershell
$env:DATABASE_URL=...
$env:REDIS_ENABLED=false
```

这些环境变量可以继续支持，但不应该成为正常本机开发流程的必要步骤。

---

## 5. Windows SQLite 功能测试矩阵

CC 必须实际执行，而不是只看 pytest。

### 5.1 启动

验证：

* 程序启动
* SQLite 自动创建
* schema 自动初始化
* migration 正常
* API 正常
* Web 页面正常
* WebSocket 正常
* 无致命异常

### 5.2 页面

实际打开并验证：

```text
/
 /setup
 /admin
 /ops
```

重点确认：

* 首页 5 秒内能判断系统状态
* 无异常时明确显示“无需操作”
* 当前模式正确
* 系统健康状态正确
* 今日发生了什么正常
* 自动恢复信息正常
* AI Review 页面/接口正常
* 配置页面正常
* 急停按钮正常
* 用户不需要操作 curl

### 5.3 配置

至少测试：

```text
读取配置
↓
修改配置
↓
保存
↓
数据库持久化
↓
自动重载/重启
↓
重新读取
```

重点检查：

```text
runtime_config
```

是否成为运行时配置真实来源。

### 5.4 模式

至少测试：

```text
模拟
测试
实盘
```

以及非法组合。

必须确认：

```text
非法配置 → fail-closed
合法配置 → 可以启动
```

绝不能为了让 UI 测试通过而降低 TradingGate。

### 5.5 风控

实际验证：

```text
正常
↓
异常
↓
阻断
↓
恢复
↓
再次判断 TradingGate
```

特别确认：

> 解冻 ≠ 自动允许交易。

这是当前系统重要安全契约，不能破坏。

---

## 6. 第二阶段：Windows Docker 准生产改造

这是本任务的重点。

当前 `docker-compose.yml` 仍然把：

```yaml
DATABASE_URL: sqlite+aiosqlite:////app/data/adaptive.db
```

直接写死在 Compose 中。

这与正式的：

```text
Docker + MySQL + Redis
```

目标冲突。

必须修改。

### 6.1 Compose 正式结构

推荐：

```text
docker compose
├── adaptive-trading
├── mysql
└── redis
```

至少做到：

```text
mysql:
  image: mysql:8.0
  persistent volume

redis:
  image: redis:7
  persistent volume/必要配置

adaptive-trading:
  depends_on:
    mysql:
      condition: service_healthy
    redis:
      condition: service_healthy
```

不要只用：

```yaml
depends_on:
  - mysql
  - redis
```

必须考虑真正的健康状态。

---

## 7. Docker 环境变量重新整理

拆清楚：

### Windows SQLite

```text
DATABASE_URL=sqlite+aiosqlite:///...
REDIS_ENABLED=false
```

### Windows Docker

```text
DATABASE_URL=mysql+pymysql://...
REDIS_ENABLED=true
REDIS_URL=redis://redis:6379/...
```

### Pi Docker

使用完全相同的：

```text
MySQL
Redis
```

只是：

```text
宿主机目录
密钥
API_HOST
资源参数
```

不同。

不要继续出现：

```text
Docker = SQLite
Pi = MySQL
```

这种架构漂移。

---

## 8. Docker MySQL

正式验证：

```text
MySQL 8
↓
创建 adaptive_trading
↓
应用启动
↓
自动 migration
↓
29 张表
↓
schema version 正确
```

必须测试：

```text
第一次启动
↓
停止
↓
再次启动
↓
数据仍存在
```

以及：

```text
docker compose down
↓
docker compose up -d
↓
数据仍存在
```

注意：

> 不允许因为 Docker 重建容器导致数据库数据消失。

---

## 9. Redis

确认真实用途之后进行验证。

如果最终确定：

```text
Redis = REQUIRED
```

则必须测试：

```text
正常启动
↓
Redis 正常
↓
adaptive-trading 正常

Redis 停止
↓
adaptive-trading 必须进入明确异常状态
↓
不能继续假装正常交易
```

恢复：

```text
Redis 恢复
↓
系统自动恢复/重新连接
↓
健康状态恢复
```

必须把 Redis 的真实依赖结论写进：

```text
docs/architecture.md
docs/verification/docker-verification.md
docs/runbook.md
```

---

## 10. Docker 完整生命周期测试

必须实际执行：

```text
docker compose build

docker compose up -d

health

homepage

API

WebSocket

MySQL

Redis

migration

配置

模式

restart

down

up

数据持久化
```

必须至少验证：

```text
应用容器 restart
数据库数据不丢

MySQL restart
应用能正确处理

Redis restart
应用能正确处理

整个 compose down/up
系统恢复正常
```

---

## 11. 系统稳定运行验证

Windows Docker 启动后，不要立即宣布准生产完成。

至少先进行：

```text
1 小时稳定运行
```

如果 1 小时通过，再根据实际情况决定是否执行：

```text
6 小时
24 小时
```

重点观察：

```text
进程是否退出
容器是否重启
MySQL 是否异常
Redis 是否异常
WebSocket 是否断开
WebSocket 是否自动恢复
Binance 行情是否正常
任务是否持续运行
内存是否持续增长
CPU 是否异常
数据库连接是否泄漏
Redis 连接是否泄漏
operator event 是否持续正常
reconciliation 是否正常
TradingGate 是否正常
是否出现 UNKNOWN order
是否出现重复订单
是否出现异常自动恢复
```

不要因为：

```text
health = 200
```

就宣布稳定。

---

## 12. Evidence Chain 独立验收

当前仓库已经明确记录：

```text
EVIDENCE_CHAIN = NOT independently executed
```

这一项必须补齐。

实际执行：

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

必须能够通过 trace id / correlation id 串起来。

至少测试：

```text
BUY
SELL
重复 Signal
重复 clientOrderId
Order timeout
UNKNOWN
Recovery
Kill
Restart
Reconciliation
```

要求：

> 不是“单元测试覆盖了”，而是实际运行链路可以找到完整证据。

最终形成：

```text
EVIDENCE_CHAIN = PASSED
```

或者如实：

```text
BLOCKED / FAILED / NOT_EXECUTED
```

禁止写成“理论上通过”。

---

## 13. 用户功能验收

当 Windows Docker 达到：

```text
DOCKER_HEALTH = PASSED
DOCKER_PERSISTENCE = PASSED
DOCKER_MYSQL = PASSED
DOCKER_REDIS = PASSED
STABILITY = PASSED
```

CC 必须：

### 13.1 在 Windows 本机正式启动 Docker 服务

例如：

```text
http://localhost:8800
```

或者实际映射端口。

### 13.2 输出用户访问地址

最终告诉用户：

```text
Windows 准生产服务已启动

访问地址：
http://localhost:8800
```

用户只需要：

```text
打开浏览器
查看页面
操作页面
```

不要让用户执行：

```text
curl
docker logs
docker exec
mysql
redis-cli
```

除非验收失败需要进一步排查。

---

## 14. 用户验收项目

用户只需要检查：

### 首页

```text
系统状态是否一眼看懂
今天发生了什么是否正常
是否明确告诉我是否需要操作
```

### Setup

```text
配置是否容易理解
当前模式是否明确
```

### Admin

```text
参数是否正常
保存后是否自动生效
```

### Ops

```text
系统健康是否容易理解
异常是否有原因
```

### 自动恢复

模拟异常后：

```text
系统是否自己恢复
恢复失败时是否明确告诉用户需要人工处理
```

### 核心体验

最终希望达到：

```text
打开页面
↓
5 秒知道系统是否正常
↓
正常时
“无需操作”
↓
异常时
明确告诉：
发生了什么
为什么
系统正在做什么
我需要做什么
```

---

## 15. 不要在本阶段做的事情

明确禁止 CC 在这个任务中顺手扩展：

```text
❌ 新交易策略
❌ 新币种
❌ Futures
❌ 高频交易
❌ 多交易所
❌ LLM 自动下单
❌ AI 直接调用 ExecutionEngine
❌ 大规模重构
❌ 微服务
❌ Kubernetes
❌ 复杂监控平台
❌ 云部署
```

如果发现与当前任务无关的问题：

```text
记录
↓
分类
↓
加入后续 TODO
```

不要擅自扩大任务范围。

---

## 16. 代码审查重点

本阶段除了部署，还要顺手检查以下问题：

### P0

```text
Docker SQLite 与正式 MySQL 架构冲突
Redis optional 与正式依赖定义冲突
配置文件 / 环境变量 / DB 优先级
MySQL migration
Redis startup/reconnect
Docker healthcheck
数据持久化
```

### P1

```text
EVIDENCE_CHAIN
订单幂等
UNKNOWN order
restart recovery
reconciliation
自动恢复
关键任务重启
TradingGate
```

### P2

```text
文档漂移
无效配置
重复配置
历史 TODO
脚本易用性
Windows 开发体验
```

---

## 17. 文档必须同步维护

本阶段修改代码后，必须同步检查：

```text
README.md
CLAUDE.md
docs/progress.md
docs/architecture.md
docs/runbook.md
docs/verification/local-verification.md
docs/verification/docker-verification.md
docs/verification/pi-deployment.md
docs/product/operator-experience.md
docs/product/unattended-operation.md
docs/ai-review-spec.md
```

重点把环境定义统一成：

```text
Level 1
Windows + Python + SQLite

Level 2
Windows + Docker + MySQL + Redis

Level 3
Pi + Docker + MySQL + Redis
```

不要再出现：

```text
Docker production = SQLite
```

这种旧口径。

---

## 18. 状态必须严格使用事实状态

所有验证结果必须使用：

```text
IMPLEMENTED
READY_TO_RUN
EXECUTED
PASSED
FAILED
BLOCKED
NOT_EXECUTED
```

特别禁止：

```text
“理论可行”
“应该没问题”
“看起来通过”
“基本完成”
“生产可用”
```

来替代真实验证结果。

---

## 19. 最终验收表

最终在 `docs/progress.md` 增加本阶段结果：

```text
WINDOWS_SQLITE_FUNCTIONAL    = PASSED / FAILED

WINDOWS_UI                   = PASSED / FAILED

DOCKER_BUILD                 = PASSED / FAILED

MYSQL                        = PASSED / FAILED

REDIS                        = PASSED / FAILED

DOCKER_HEALTH                = PASSED / FAILED

DOCKER_PERSISTENCE           = PASSED / FAILED

DOCKER_CONFIG                = PASSED / FAILED

DOCKER_MODE                  = PASSED / FAILED

DOCKER_RECOVERY              = PASSED / FAILED

EVIDENCE_CHAIN               = PASSED / FAILED / NOT_EXECUTED

STABILITY_1H                 = PASSED / FAILED

USER_UI_ACCEPTANCE           = PASSED / FAILED

WINDOWS_PRE_PRODUCTION       = YES / NO

READY_FOR_PI                 = YES / NO
```

注意：

```text
WINDOWS_PRE_PRODUCTION = YES
```

必须同时满足核心验收项。

---

## 20. Pi 阶段暂不执行

本任务完成后只达到：

```text
Windows 准生产
```

然后才进入：

```text
Level 3 — Pi
```

Pi 的目标已经确定：

```text
Raspberry Pi ARM64
Docker
MySQL
Redis
LAN
无人值守
长时间运行
```

Pi 阶段重点：

```text
ARM64 镜像
MySQL 持久化
Redis
重启恢复
断电恢复
网络恢复
WebSocket 恢复
长时间稳定性
无人值守
```

本任务不要因为“以前 Pi 曾经部署过”就直接判定生产完成。

---

## 21. Git / 提交 / 远程仓库

每完成一个明确工作单元：

```text
检查 git diff
↓
ruff
↓
mypy
↓
pytest -m "not testnet"
↓
必要的真实环境验证
↓
更新文档
↓
commit
↓
push origin/main
```

遵守现有 `CLAUDE.md` 的提交约定：每个工作单元独立 commit + push，不攒任务。

不要提交：

```text
.env
密钥
*.db
logs/
evidence/
reports/
```

---

## 22. 最重要：完成后保存进度

任务结束时，CC 必须主动：

### ① 保存进度

更新：

```text
docs/progress.md
```

明确记录：

```text
做了什么
实际执行了什么
实际通过什么
失败什么
阻塞什么
没有执行什么
下一阶段是什么
```

### ② 维护相关文档

不要只修改 progress。

凡是环境、架构、启动方式、Docker、MySQL、Redis、验证方式发生变化，必须同步：

```text
README.md
CLAUDE.md
docs/architecture.md
docs/runbook.md
docs/verification/*
```

### ③ Commit

每个工作单元完成后独立 commit。

### ④ Push

完成后：

```bash
git push origin main
```

### ⑤ 最终报告必须给出

```text
最终 Git SHA
push 是否成功
Windows SQLite 是否通过
Windows Docker 是否通过
MySQL 是否通过
Redis 是否通过
稳定性是否通过
用户验收是否完成
READY_FOR_PI = YES / NO
```

**不要声称 push 成功，除非实际执行并拿到结果。**

---

# 本阶段最终目标

不是继续“开发更多功能”。

而是把系统从：

```text
代码基本完成
```

推进到：

```text
Windows SQLite
    ↓
功能真实通过
    ↓
Windows Docker
    ↓
MySQL + Redis
    ↓
稳定运行
    ↓
用户浏览器验收
    ↓
Windows 准生产完成
```

只有这个链路真正跑通后，才进入：

```text
Pi Docker
↓
MySQL + Redis
↓
长时间无人值守
↓
小资金验证
```

最终仍然遵守：

> **先验证系统，再验证无人值守，再验证小资金，最后才考虑扩大资金。**
