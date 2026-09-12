# Level 1 — 本机直接运行验证

> 三级验证的第一级。**只有 Level 1 + Level 2 达到高完成度，才允许进入 Level 3（Pi）。**
> 状态词严格区分「代码写完」「配置已存」「服务已跑」「实际验证过」。

## 怎么跑

```bash
# 本机启动(无 MySQL/Redis, 用 SQLite 覆盖)
DATABASE_URL='sqlite+aiosqlite:///./adaptive.db' REDIS_ENABLED=false python run.py
# → http://127.0.0.1:8800
```

## 验证矩阵

| 项 | 命令 / 端点 | 状态 |
|----|------------|------|
| 静态检查 | `ruff check .` / `mypy` | **PASSED** |
| 单元 + 集成测试 | `pytest -q -m "not testnet"` | **PASSED**（1557） |
| HTTP `/api/health` | `curl :8800/api/health` | **PASSED** |
| HTTP `/api/trading-mode` | 返回三模式 + 可启动性 | **PASSED** |
| HTTP `/api/trading-mode/preview` | 不写盘, 返回 diff + 守卫预检 | **PASSED** |
| HTTP `/api/trading-mode/apply` | 只保存 + 提示重启 | **PASSED** |
| HTTP `/api/operator-status` | 三模式字段 + 买卖许可 | **PASSED** |
| 模式切换跨重启 | `tests/integration/test_v129_mode_switch_reload.py` | **PASSED**（7 条） |
| SQLite `runtime_config` 持久化 | 写入后重启仍在 | **PASSED** |
| 交易安全闭环（纸面） | BUY/SELL/重复信号/幂等/UNKNOWN/急停/对账漂移 | **PASSED**（既有单测） |
| 真实测试网 | `pytest -m testnet` | **NOT_EXECUTED** |

## 本机已修复的真实故障（V12.9）

**现象**：页面切换模式后，**重启服务起不来**（不是切换报错，是切换后重启即死）：

```
RuntimeError: 运行模式解析失败: 配置冲突:
  paper_trading 显式设为 True, 但 TRADING_MODE=testnet 要求 False
```

**成因**：`.env` 里遗留 `PAPER_TRADING=true`（迁移前的显式值），DB 里有
`TRADING_MODE=testnet`（页面切换写入）。DB 覆盖生效后 `trading_mode=testnet`，
但**冲突检查**仍把那条**已被取代的** `.env` 值当成操作者意图 → 判冲突 → 拒绝启动。

**修复**：`TRADING_MODE` 被 DB 覆盖时，它**推导出的**字段一并从冲突判定中剔除。
**没有放宽冲突检测** —— 同层（都在 `.env`）矛盾时照常 fail-closed，有测试锚定。

**回归**：`tests/integration/test_v129_mode_switch_reload.py`（7 条），走
「env 造 Settings → 叠加 DB 覆盖 → 排除被覆盖字段 → resolve → 断言」全链路，
不只测单函数。

## 本机与 Docker 的分工

Level 1 验证**代码与配置语义**（模式解析、切换、持久化、守卫链）。
Level 2 验证**运行时载体**（镜像、卷、重启、健康检查）。
两者都要过，才谈 Pi。
