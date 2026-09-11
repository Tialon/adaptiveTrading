# CC 执行任务 — 个人管理页面 / 模式切换 / 开关与参数配置（2026-09-12）

> 目标：为个人内网使用场景新增一个易用的管理页面，让操作者可以折叠查看当前配置解释，并在页面内完成运行模式切换、开关选择、参数配置和功能说明。
>
> 范围：面向单人本地内网使用，默认不做多用户权限系统、不做公网安全设计、不展开密钥管理问题。仍必须保留现有交易安全闸门，不得绕过 `TradingGate`、主网守卫、测试网守卫或 `settings.validate()`。

## P0：上线前代码检查

先执行：

```bash
git status --short
uv run ruff check .
uv run mypy
uv run pytest -q --cov --cov-report=term-missing --cov-fail-under=75 -m "not testnet"
```

验收：

- 无未解释代码改动；不得提交 `.env`、密钥、数据库、日志或证据文件。
- `ruff`、`mypy`、非 testnet 测试通过，coverage ≥ 75%。
- 将检查结果写入 `docs/progress.md`。

## P1：新增个人管理页面

新增管理页面，建议路径：

```text
/admin
```

页面定位：

- Dashboard `/` 保持“看状态”。
- `/admin` 用于“改配置、切模式、看开关解释、执行管理操作”。
- 页面为个人内网使用，不做复杂权限体验；如果写接口仍需要 `X-Admin-Token`，页面提供本地输入框并保存在浏览器 `localStorage`，不回传、不展示明文。

验收：

- `/admin` 可以从 Dashboard 顶部入口进入。
- `/admin` 页面首屏能看到当前模式、运行状态、是否允许买入/卖出。
- 没有配置写操作 token 时，页面仍可读；写操作按钮显示“需要管理令牌”或“写操作未启用”。

## P2：当前配置解释可折叠

把“当前配置解释”做成可折叠区块，默认折叠，标题显示一句摘要：

```text
当前：纸面 + 测试网，真实资金不会被使用
```

展开后显示关键开关及作用：

| 开关 | 作用 | 当前值展示 |
|------|------|------------|
| `PAPER_TRADING` | 是否纸面模拟成交；为 true 时不会真实下单 | `true/false` |
| `BINANCE_TESTNET` | 是否连接 Binance 测试网；为 false 时连接主网 | `true/false` |
| `LIVE_TRADING_CONFIRM` | 主网连接确认；主网模式必须显式 true | `已确认/未确认` |
| `MAINNET_API_SCOPE_CONFIRM` / `MAINNET_API_SCOPE_CONFIRMED` | 主网 API 权限人工确认 | `已确认/未确认` |
| `WEB_ADMIN_TOKEN` | 是否启用管理写操作 | `已配置/未配置`，不得显示真实值 |
| `AI_ENABLED` | 是否启用 AI 建议；AI 只建议不自动下单 | `true/false` |
| `DAILY_REPORT_ENABLED` | 是否生成每日复盘 | `true/false` |
| `STARTUP_RECONCILE_ENABLED` | 实盘启动时是否做启动对账 | `true/false` |
| `MAINNET_TAKEOVER_ENABLED` | 主网首次只读接管是否启用 | `true/false` |

验收：

- 配置解释默认折叠，不占首屏太多空间。
- 展开后每个开关都有“功能作用”和“当前值”。
- 敏感值只显示状态，不显示真实内容。
- 移动端展开后不横向溢出。

## P3：运行模式切换

在 `/admin` 增加“运行模式”选择，建议使用单选卡片或分段控件：

```text
纸面模式：PAPER_TRADING=true
测试网真实：PAPER_TRADING=false + BINANCE_TESTNET=true
主网纸面观察：PAPER_TRADING=true + BINANCE_TESTNET=false
主网真实：PAPER_TRADING=false + BINANCE_TESTNET=false + LIVE_TRADING_CONFIRM=true
```

实现要求：

- 后端新增配置草稿接口，不直接改 `.env` 后立刻重启交易。
- 推荐新增：

```text
GET  /api/admin/config
POST /api/admin/config/draft
POST /api/admin/config/apply
```

- `GET /api/admin/config` 返回当前配置摘要和可编辑字段定义。
- `POST /api/admin/config/draft` 只校验并返回 diff、风险提示、是否需要重启。
- `POST /api/admin/config/apply` 才写入配置文件或持久化配置，并提示用户重启服务。

验收：

- 切换模式前显示将变化的开关列表。
- 主网真实模式必须出现明显红色确认提示。
- 不允许通过管理页面绕过 `LIVE_TRADING_CONFIRM` 和 `MAINNET_API_SCOPE_CONFIRM`。
- 应用后页面显示“配置已保存，需重启容器生效”或“已热生效”，不能让用户误以为一定即时生效。

## P4：开关选择区

在 `/admin` 增加开关选择区，按类别分组：

运行类：

- `PAPER_TRADING`
- `BINANCE_TESTNET`
- `DAILY_REPORT_ENABLED`
- `AI_ENABLED`

安全类：

- `STARTUP_RECONCILE_ENABLED`
- `MAINNET_READINESS_ENABLED`
- `MAINNET_TAKEOVER_ENABLED`

运维类：

- Web 管理写操作是否可用，只展示 `WEB_ADMIN_TOKEN` 状态。
- 日志级别 `LOG_LEVEL`。

验收：

- 每个开关旁边有一句短说明。
- 每个开关旁边有“推荐值”提示。
- 高风险开关关闭时显示橙色或红色警告。
- 对个人使用场景，页面文案保持简单，不使用过多安全术语。

## P5：参数配置区

在 `/admin` 增加参数配置表单，第一版只开放低风险、常用、可解释的参数。

建议开放：

交易与风控：

- `SYMBOLS`：当前冻结为 `SOLUSDT`，只读展示。
- `RISK_MAX_SINGLE_ORDER_PCT`：单笔最大占比。
- `RISK_MAX_POSITION_PCT`：策略仓位上限。
- `RISK_MAX_SOL_EXPOSURE`：SOL 总敞口上限。
- `RISK_MAX_DAILY_LOSS`：日内亏损阈值。
- `RISK_MAX_DRAWDOWN`：最大回撤急停阈值。

策略：

- `ENTRY_BUY_THRESHOLD`：买入评分阈值。
- `ENTRY_OBSERVE_THRESHOLD`：观察阈值。
- `SELL_TAKE_PROFIT_LADDER`：分批止盈阶梯。
- `BUY_DIP_PCT`：相对 VWAP 折价买入阈值。
- `SELL_PROFIT_PCT`：基础止盈比例。

运行：

- `RECONCILE_INTERVAL_SECONDS`：对账间隔。
- `PORTFOLIO_REBALANCE_INTERVAL_SECONDS`：组合检查间隔。
- `DAILY_REPORT_DIR`：日报目录，只读或谨慎编辑。

验收：

- 每个参数显示：名称、当前值、单位、作用、推荐范围、是否需要重启。
- 百分比参数用百分比输入，不让用户直接猜 `0.05` 代表什么。
- 保存前做前端基础校验，后端再用 `settings.validate()` 或等价校验兜底。
- 参数错误时逐项提示，不只返回“配置失败”。

## P6：配置保存与回滚

实现配置写入时必须有回滚能力：

- 写入前备份当前配置为 `/etc/adaptive-trading/production.env.bak.<timestamp>` 或仓库外等效路径。
- 保存后记录变更摘要，不记录敏感值。
- 提供“恢复上一份配置”按钮或命令提示。

验收：

- 保存配置不会把密钥写入仓库。
- diff 中敏感字段只显示 `<configured>`、`<empty>`、`<changed>`。
- 恢复上一份配置后可重新渲染 Compose 配置。

## P7：管理操作区

在 `/admin` 放置管理操作，保持二次确认：

- 急停：`POST /api/emergency/kill`
- 恢复急停：`POST /api/emergency/recover`
- 解除熔断：`POST /api/breaker/reset`
- 请求优雅停机：`POST /api/shutdown`

验收：

- 急停按钮常驻且醒目。
- 恢复、解除熔断、停机必须二次确认。
- 确认框展示当前模式、当前状态、操作后果。
- 主网真实模式下确认文案必须明确“主网真实资金”。

## P8：部署检查页与管理页合并入口

保留或新增 `/ops`，但从 `/admin` 中提供入口：

- `/admin`：配置与管理。
- `/ops`：上线前检查，只读。

`/ops` 显示：

- App version / Git SHA / Image tag。
- 当前模式与运行状态。
- `/api/health` 是否成功。
- `/api/metrics.health` 是否可读。
- 写操作是否启用。
- 最近错误。
- 最近对账时间。

验收：

- `/ops` 不包含危险按钮。
- `/ops` 给出 `PASS / WARN / BLOCKED`。
- `/admin` 能跳转 `/ops`，用于部署前自检。

## P9：测试与文档

补测试：

- `/api/admin/config` 未启动时也能返回配置摘要。
- 配置草稿 diff 正确隐藏敏感值。
- 四种模式切换 draft 输出正确。
- 非法百分比、非法阈值、非法止盈阶梯会被拒绝。
- 主网真实模式不能绕过现有确认字段。

补文档：

- 更新 `README.md`：说明 `/`、`/admin`、`/ops` 三个入口。
- 更新 `docs/operating-modes-manual.md`：补“管理页面如何理解和切换模式”。
- 更新 `docs/runbook.md`：补“配置保存后如何重启 / 如何回滚”。
- 更新 `docs/progress.md`：记录实现内容、测试命令和结论。

最终验收命令：

```bash
uv run ruff check .
uv run mypy
uv run pytest -q --cov --cov-report=term-missing --cov-fail-under=75 -m "not testnet"
```

手工验收：

- Dashboard 首屏不被配置说明占满。
- “当前配置解释”默认折叠，展开后能看懂每个开关作用。
- `/admin` 可以选择模式、编辑常用参数、保存草稿、查看 diff。
- 保存配置后明确提示是否需要重启。
- `/ops` 能完成上线前只读检查。
- 手机宽度下无文字重叠、按钮溢出或表单挤压。

## 禁止项

- 不允许新增绕过后端闸门的前端交易入口。
- 不允许在页面展示密钥明文。
- 不允许把个人内网使用理解为可以移除现有主网守卫。
- 不允许关闭 `settings.validate()` 的配置校验。
- 不允许把配置保存失败伪装成成功。
