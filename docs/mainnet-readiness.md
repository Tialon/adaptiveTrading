# 主网就绪自检与上线复审(Mainnet Readiness)

> V11.8 §21-§29 交付。本文是「主网上线前最终安全审计」的**人工 go/no-go 复审**清单,
> 与代码内 `at01_common/mainnet_readiness.py::mainnet_readiness_check` 配合:
> 代码层做**确定性本地判定**(配置/环境/开关), 本文做**非确定性人工确认**(API 权限/资金/审计),
> 二者缺一不可。**绝对禁止跳过本复审直接开主网。**

## 1. 两道闸门

| 闸门 | 层 | 性质 | 谁执行 |
|------|----|------|--------|
| `MAINNET_READINESS_CHECK` | 代码 | 确定性: 配置/开关/端点/git_sha | 启动时自动, 任一不满足 → BLOCKED 拒绝启动 |
| 本文档复审 | 人工 | 非确定性: API 权限/资金/风险限额/审计 | 操作者逐项核对, 全绿才允许主网 |

**代码闸门八项**(`mainnet_readiness_check`, 见 `test_v175_mainnet_readiness.py`):

1. `BINANCE_TESTNET=false`(确连主网); 2. `PAPER_TRADING=false`(非纸面);
3. `LIVE_TRADING_CONFIRM=true`; 4. `MAINNET_API_SCOPE_CONFIRM=true`;
5. `symbol` = `SOLUSDT`; 6. `config_problems` 空; 7. `kill_switch_armed=false`; 8. `git_sha` 非空
且 `base_url` 含 `api.binance.com` 不含 `testnet`。

`MAINNET_API_SCOPE_CONFIRM` 默认 `false` → 主网默认必 BLOCKED; Binance 无法通过 API 自证 key
权限, 故须操作者核对后显式置 `true`。

## 2. 人工复审清单(上线前逐项核对)

### A. API 权限(必须人工, 无法自证)

- [ ] 主网 API key 仅授权 **Spot 交易**(`Enable Spot Trading`), **关闭提现**(`Enable Withdrawals` = OFF)。
- [ ] **关闭资金转移/内部转账**权限; 不启用 Futures / Margin / Fiat 存取。
- [ ] IP 白名单已配置(仅部署机公网 IP); 或至少 key 已限流且未共享。
- [ ] key 从不在仓库/日志/任何 artifact 中出现(`.env` 已 gitignore)。

### B. 资金与风险限额

- [ ] 首期资金为**极小资金**(计划金额, 见 mainnet-runbook), 明确「可承受全部亏损」。
- [ ] `RISK_MAX_POSITION_PCT` / `RISK_MAX_SINGLE_ORDER_PCT` / `RISK_MAX_DAILY_LOSS` /
  `RISK_MAX_DRAWDOWN` 已在 `.env` 明确(默认 40%/5%/5%/15%), 首期建议再收紧。
- [ ] 交易所侧若有单日/单笔限额(非本项目能力), 已人工设置。

### C. 运行环境

- [ ] `git_sha` 与发布镜像 tag 一致(镜像 tag = `adaptive-trading:<git_sha>`, 可追溯)。
- [ ] `BINANCE_TESTNET=false` + `base_url=https://api.binance.com`(无 testnet 串)。
- [ ] DB 为持久化卷(SQLite 在 `./data`), 启动对账 + 周期对账 + 急停持久化全部启用。
- [ ] kill switch 未被冻结(`kill_switch_state` 非 armed), 或已知冻结并已人工解除。

### D. 已通过的前置验证(诚实核对)

- [ ] 测试网真实下单闭环已 PASSED(见 testnet-operation.md), 非纸面。
- [ ] 测试网 7h/24h soak 已 PASSED(L3 达成)。
- [ ] 故障恢复验证已 PASSED(急停/重启/崩溃/对账)。
- [ ] 财务一致性验证已 PASSED(交易所真相 + 交叉对账 + 权益对账)。
- [ ] 全量测试全绿 + coverage ≥ 75% + ruff 全绿(以当前 HEAD 实跑为准; 当前 HEAD 为 1448 passed)。

### E. 停机/应急

- [ ] 知道如何急停: `POST /api/emergency/kill`(需 `X-Admin-Token`)。
- [ ] 知道如何人工撤单(交易所侧)并停止容器(`docker compose stop`)。
- [ ] `WEB_ADMIN_TOKEN` 已设非空且妥善保管(非回环 `API_HOST=0.0.0.0` 时强制)。

## 3. go/no-go 结论模板

```text
[日期] 主网上线复审
- 代码闸门: MAINNET_READINESS_CHECK = ALLOWED(启动时打印 === MAINNET READINESS === 全绿)
- API 权限: Spot only + 关提现 = 确认
- 资金: 极小资金 <amount>, 限额已设
- 前置: L3(7h/24h soak) = PASSED / 未达成(未达成则 GO=NO)
- 结论: GO / NO-GO
```

**任一人工项未确认 → NO-GO**, 不因代码闸门通过而开主网。

## 4. 与代码闸门的关系(避免重复/遗漏)

- 代码闸门(`mainnet_readiness_check`)在 `wiring.py` 主网守卫之后、测试网闸门之前执行;
  `BINANCE_TESTNET=false` 时生效, 打印报告, 任一不满足 `raise RuntimeError`。
- `kill_switch_armed` 在代码闸门传 `False`(持久化态尚未载入), 由启动末尾 `kill_switch.is_armed`
  单独守卫兜底 —— 故「急停冻结」这一维实际由启动末尾守卫落实, 本文 D 项人工再核一次。
- 代码闸门不做、也做不了: API 权限、IP 白名单、资金额度、人工审计 —— 这些只在本文 §2 人工核对。
