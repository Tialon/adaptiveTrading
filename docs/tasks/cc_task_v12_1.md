# CC 执行任务 — V12.1 工程收口（2026-09-09）

> 基线：`main` @ `cfa552a`；产品边界不变（Binance / SOLUSDT 现货 / 双仓 / 低频 / AI 只提案）。
> 当前结论：代码功能面仍为「主网未上线」，不得因本任务通过而改变主网 go/no-go 结论。

## 执行顺序与验收

### P0 — 恢复 CI 绿灯（先做）

`ruff check .` 当前失败，两处均为测试中的未使用导入：

1. 删除 `tests/unit/test_v12_db_backup.py` 的 `import pytest`。
2. 删除 `tests/unit/test_v12_risk_params.py` 的 `TieredDrawdownManager` 导入（保留 `TIERS`）。

验收：`uv run ruff check .` exit 0。不要扩大 Ruff 规则范围，也不要用全局 ignore 掩盖问题。

### P1 — 收口执行/恢复链路的可空值契约

`uv run mypy` 当前在 6 个核心文件报 24 个错误；多数来自 `self.rest` 或
`self.execution` 为 `Optional` 后，守卫与实际调用之间缺少局部非空绑定。按以下方式修：

1. 在 `startup_reconciler.py`、`order_recovery.py`、`execution_executor.py` 中，每个需交易所查询
   的入口先 fail-closed：依赖不存在时返回未解决/抛出受控错误，保持禁开仓；守卫后绑定局部变量
   （例如 `rest = self.rest`）再调用，不能靠 `cast(Any, ...)` 或 `# type: ignore` 消音。
2. `execution_executor.py:1087`：在调用 `_ingest_fills` 前，要求 `exchange_order_id` 非空；若恢复订单
   没有交易所 ID，只能使用订单级数据并留下可审计日志，不能把 `None` 传到查询接口。
3. `execution_state.py:169`：修正 `set?[str]` 的类型标注为合法的 `set[str]`（或等价标准泛型）。
4. `exchange_truth_reconciler.py:154-155`：过滤/拒绝空时间戳后再计算最小值及 UTC 转换。
5. `cross_reconciler.py:95`：显式解析数值字段；遇到空值或非数值时进入现有的安全异常/差异路径，不能静默当作 0。

必须补测：

- REST 或 execution 依赖缺失时，恢复/启动对账不会 AttributeError，且交易保持 fail-closed；
- `exchange_order_id=None` 的恢复订单不触发查询、不会伪造成交；
- 空/非法时间戳与金额进入对账时得到可审计的不一致结果而不是崩溃。

验收：`uv run mypy` 0 errors（现有 annotation-unchecked note 可保留）；新增测试通过。

### P2 — 用 CI 同款命令完成回归证据

执行：

```powershell
uv run pytest -q --cov --cov-report=term-missing --cov-fail-under=75 -m "not testnet"
```

验收：全绿、coverage ≥ 75%。不要把 testnet 标记测试改为自动执行；真实测试网/主网动作不在本任务授权范围内。

## 完成后的必做收口

1. 更新 `docs/progress.md` 顶部：记录 commit、精确 pytest/coverage、ruff/mypy 结果，以及「主网仍未上线」。
2. 更新 `README.md` 的版本、文档索引和测试命令，避免继续指向 V11.8 / 旧 `cc_task.md`。
3. 在本文件末尾增加“执行记录”：命令、结果、未执行的外部验证和剩余风险。
4. **保存进度信息**：在提交前确认 `git status --short`，只纳入本任务文件；提交信息说明 V12.1 工程收口；推送 `main` 后记录 commit SHA 与远程同步结果。

## 本轮审查证据

- `pytest --collect-only -q`：1295 tests collected（包含 opt-in testnet；真实测试未在本轮触发）。
- `ruff check .`：2 个 F401，均在测试文件。
- `mypy`：24 errors / 6 files，集中在执行、订单恢复和启动对账的 Optional 依赖与外部字段解析。
- 工作树原有未追踪 `.claude/`：保留本地，不得提交。

---

## 执行记录（2026-09-09）

### 命令与结果

| 命令 | 结果 |
|------|------|
| `uv run ruff check .` | exit 0（P0 删除两处未用导入后） |
| `uv run mypy` | 0 errors（原 24 errors / 6 files 全部收口，annotation-unchecked note 保留） |
| `uv run pytest -q --cov --cov-report=term-missing --cov-fail-under=75 -m "not testnet"` | **1298 passed, 6 deselected**；coverage **79.92%** ≥ 75% |

### 改动文件

- 修复：`at50_execution/startup_reconciler.py`、`order_recovery.py`、`execution_executor.py`、
  `execution_state.py`、`exchange_truth_reconciler.py`、`cross_reconciler.py`
- 测试：`tests/unit/test_v12_db_backup.py`、`test_v12_risk_params.py`（删未用导入）、
  `tests/unit/test_v121_null_contract.py`（新增 9 条）
- 文档：`docs/progress.md`、`README.md`、`cc_task_v12_1.md`

### 未执行的外部验证

- 真实测试网交易（7h/24h soak）与主网只读接管 / 极小资金 BUY/SELL：**未执行**（超出本任务授权范围）。
- ARM64（树莓派）Docker 构建 + CI docker-smoke：未在本轮重跑。

### 剩余风险

- **主网仍未上线**：本项目功能面维持「代码就绪 / 主网未上线」，本轮收口不改变主网 go/no-go 结论。
- 可空值契约的 fail-closed 守卫以静态类型 + 单测锁定；真实环境缺失依赖的运行时路径未经测试网实证。
- `annotation-unchecked` note（untyped 函数体不检查）仍存在，属既有宽松检查策略，非本轮新增。
