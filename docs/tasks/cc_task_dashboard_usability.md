# CC 执行任务 — Dashboard 易用性 / 状态展示 / 开关提示优化（2026-09-12）

> 目标：让操作者打开 Dashboard 后，能在 5 秒内判断“当前是什么运行模式、能不能交易、为什么不能、下一步该做什么”，并降低急停、恢复、熔断解除等写操作的误点风险。
>
> 范围：只优化 Web/API 展示层与少量只读聚合接口，不改变交易策略、不改变风控判定、不放宽主网守卫、不新增交易能力。

## P0：上线前代码检查

先执行并记录结果：

```bash
git status --short
uv run ruff check .
uv run mypy
uv run pytest -q --cov --cov-report=term-missing --cov-fail-under=75 -m "not testnet"
```

验收：

- 无未解释代码改动；不得提交 `.env`、密钥、数据库、日志、证据文件。
- `ruff`、`mypy`、非 testnet 测试全部通过，coverage ≥ 75%。
- 将检查结果写入 `docs/progress.md`。

## P1：新增操作者状态聚合接口

新增只读接口，建议路径：

```text
GET /api/operator-status
```

建议在 `at10_web/web_api_routes.py` 中实现，复用 `build_runtime_health(system_state)`，不要另起一套交易判定。

返回字段建议：

```json
{
  "mode": "paper_testnet | live_testnet | paper_mainnet | live_mainnet",
  "mode_label": "纸面模式 / 测试网真实 / 主网只读或实盘",
  "risk_level": "safe | warning | danger | blocked",
  "status": "SAFE | TRADING | DEGRADED | REDUCE_ONLY | PAUSED | RECOVERY | KILLED",
  "can_buy": false,
  "can_sell": true,
  "summary": "当前禁止买入：行情数据不健康",
  "next_action": "检查行情连接或等待恢复",
  "write_actions_enabled": true,
  "dangerous_actions": ["breaker_reset", "emergency_recover"]
}
```

模式判定从 settings 读取：

- `paper_trading=true` + `binance_testnet=true` → `paper_testnet`
- `paper_trading=false` + `binance_testnet=true` → `live_testnet`
- `paper_trading=true` + `binance_testnet=false` → `paper_mainnet`
- `paper_trading=false` + `binance_testnet=false` → `live_mainnet`

验收：

- 接口在系统未完全启动时也返回可读 JSON，不抛 500。
- `can_buy` / `can_sell` 必须直接来自 `runtime_health`，不得重复实现交易许可逻辑。
- `summary` 和 `next_action` 使用中文人话，不直接裸露内部字段名作为主要提示。
- 补单元测试覆盖四种模式、KILLED、REDUCE_ONLY、PAUSED、交易闸门未就绪。

## P2：Dashboard 顶部运行结论卡

修改 `at10_web/static/index.html`，在首屏统计卡片上方新增一条醒目的运行结论区。

必须展示：

- 当前模式：纸面 / 测试网真实 / 主网。
- 当前状态：`TRADING`、`KILLED` 等状态的人话解释。
- 买入许可：允许买入 / 禁止买入。
- 卖出许可：允许卖出 / 禁止卖出。
- 阻断原因：优先显示 `buy_block_reason`，其次显示 `sell_block_reason`、`last_error`。
- 下一步建议：来自 `/api/operator-status.next_action`。

颜色建议：

- 纸面 / 安全：绿色或灰色。
- 测试网真实：蓝色或橙色。
- 主网真实：红色。
- `KILLED` / `RECOVERY` / `PAUSED`：红色或橙色。
- `REDUCE_ONLY`：橙色，明确“只允许卖出减仓”。

验收：

- 打开 Dashboard 5 秒内能看到“当前能不能买、为什么不能”。
- `/api/metrics` 或 WebSocket 暂时无数据时，页面显示“系统启动中 / 交易闸门未就绪”，不显示空白。
- 移动端不重叠、不溢出。

## P3：运行模式横幅与开关解释

在 Dashboard 增加“当前配置解释”区域，显示关键开关的人话含义：

- `PAPER_TRADING`
- `BINANCE_TESTNET`
- `LIVE_TRADING_CONFIRM`
- `MAINNET_API_SCOPE_CONFIRM` / `MAINNET_API_SCOPE_CONFIRMED`
- `WEB_ADMIN_TOKEN`

展示示例：

```text
当前模式：纸面 + 测试网
含义：不会使用真实资金，适合 Pi 首次部署验证。
写操作：已启用 / 未启用
主网守卫：未开启，禁止主网真实交易。
```

验收：

- 操作者不需要读 `.env` 也能理解当前运行组合。
- 主网真实模式必须有明显红色提示。
- `WEB_ADMIN_TOKEN` 只展示“已配置 / 未配置”，不得展示真实值。

## P4：危险写操作二次确认

优化现有按钮：

- `POST /api/breaker/reset`
- `POST /api/emergency/kill`
- `POST /api/emergency/recover`
- `POST /api/shutdown`

前端要求：

- “急停”按钮可以常驻明显位置。
- “恢复急停”“解除熔断”“停机”必须弹出确认框。
- 确认框中展示当前模式、当前状态、操作后果。
- 主网真实模式下，确认文案必须包含“主网真实资金”。
- 请求失败时显示接口返回的 `detail` 或 `msg`，不要静默失败。

验收：

- 没有 `WEB_ADMIN_TOKEN` 时，按钮显示“写操作未启用”，点击后解释原因。
- token 错误时显示“未授权”，不让用户误以为操作成功。
- 成功后自动刷新 `/api/operator-status` 与 `/api/metrics`。

## P5：状态字典统一翻译

在前端集中维护状态翻译表，至少覆盖：

```text
SAFE         就绪/启动中：未进入交易或交易闸门未完全就绪
TRADING      运行中：可按交易闸门执行
DEGRADED     降级：禁止开新仓，允许安全离场
REDUCE_ONLY  只减仓：禁止买入，只允许卖出减仓
PAUSED       暂停：熔断或风控暂停，等待冷却或人工检查
RECOVERY     恢复核验：等待对账确认，禁止开仓
KILLED       急停冻结：重启不会自动恢复，需要人工 recover
```

验收：

- Dashboard 不再只显示英文状态作为主要提示。
- 英文状态可保留为小字或 badge，方便调试。
- 文案与 `docs/operating-modes-manual.md` 保持一致。

## P6：部署检查页

新增一个轻量页面或面板：

```text
/ops
```

用于执行 Pi 上线前快速检查。页面读取现有只读接口并显示：

- App version / Git SHA / Image tag。
- 当前模式与运行状态。
- `/api/health` 是否成功。
- `/api/metrics.health` 是否可读。
- 写操作是否启用。
- 最近错误。
- 最近对账时间。
- 数据目录、日志目录不要求从 API 暴露真实路径；如暂时没有接口，可显示“未暴露”。

验收：

- 页面只读，不包含危险按钮。
- 给出 `PASS / WARN / BLOCKED` 三态。
- 与 `cc_task_pi_root_quick_deploy.md` 的上线前检查步骤对应。

## P7：测试与文档

补充测试：

- `/api/operator-status` 未启动时返回稳定 JSON。
- 四种运行模式判定正确。
- `KILLED`、`PAUSED`、`REDUCE_ONLY` 的 `summary` 与 `next_action` 正确。
- 写操作失败时前端提示逻辑可用；如项目没有前端测试框架，至少保留可手工验证清单。

补充文档：

- 更新 `README.md` 的 Web Dashboard 说明。
- 更新 `docs/operating-modes-manual.md`，加入 Dashboard 状态区说明。
- 更新 `docs/progress.md`，记录命令、测试结果和结论。

最终验收命令：

```bash
uv run ruff check .
uv run mypy
uv run pytest -q --cov --cov-report=term-missing --cov-fail-under=75 -m "not testnet"
```

手工验收：

- 纸面模式打开 Dashboard，首屏显示“不会使用真实资金”。
- 测试网真实模式显示“测试网真实下单”。
- 主网真实模式显示红色提示。
- 急停后状态变为 `KILLED`，显示“重启不会自动恢复”。
- `WEB_ADMIN_TOKEN` 未配置时，写操作显示未启用。
- 移动端宽度下首屏无文字重叠。

## 禁止项

- 不允许放宽 `TradingGate`、`mainnet_readiness`、`testnet_gate` 或 `settings.validate()` 的安全判定。
- 不允许为了好看隐藏 `can_buy=false`、`kill_switch.armed=true`、`last_error`。
- 不允许在前端展示任何密钥值。
- 不允许新增主网交易入口。
- 不允许把 Compose health 当成“可买入”结论；交易许可只能看 `runtime_health.can_buy`。
