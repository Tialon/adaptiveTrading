# 主网启动前检查清单(Mainnet Pre-start Checklist)

> V12 §32 交付。这是**每次启动主网容器前必须逐项执行的运维清单**(确定性强, 可脚本化),
> 与 [mainnet-readiness.md](mainnet-readiness.md) 的「人工复审」(非确定性: API 权限/资金/审计)
> 互补 —— 本文管「启动前跑什么命令、核什么结果」, 那份管「上不上线的人工 go/no-go 判断」。

**总原则**: 任一「必须(REQUIRED)」项未通过 → **不得 `docker compose up -d`**。

## 0. 前置: 备份 + 完整性(§31)

```bash
# 在宿主机(Pi)执行; 先校验完整性, 再在线备份(默认保留最近 30 份到 data/backups/)
python scripts/db_backup.py --db data/adaptive.db --backup-dir data/backups --keep 30
```

- [ ] REQUIRED: 输出 `integrity_check OK` + `backup OK -> ...`(退出码 0)。
- [ ] REQUIRED: `data/backups/` 下出现新备份文件, 大小 > 0(与源库相当)。

> 首次上线若 `data/adaptive.db` 尚不存在(全新部署), 备份脚本会报「DB 不存在」并退出 1 ——
> 属预期, 可跳过本步; 但**上线后每次启动前都必须先备份**。

## 1. 配置核对(确定性)

```bash
# 在宿主机核对 .env(绝不打印到日志/仓库)
grep -E '^(PAPER_TRADING|BINANCE_TESTNET|LIVE_TRADING_CONFIRM|MAINNET_API_SCOPE_CONFIRM|SYMBOLS)=' .env
```

- [ ] REQUIRED: `PAPER_TRADING=false`、`BINANCE_TESTNET=false`(确连主网)。
- [ ] REQUIRED: `LIVE_TRADING_CONFIRM=true`、`MAINNET_API_SCOPE_CONFIRM=true`。
- [ ] REQUIRED: `SYMBOLS=SOLUSDT`(无多币种)。
- [ ] REQUIRED: `API_HOST=0.0.0.0` 时 `WEB_ADMIN_TOKEN` 非空(局域网 Dashboard 的写接口鉴权兜底)。

## 2. API 权限人工确认(§8, 无法自证)

- [ ] REQUIRED: 主网 key 仅授权 **Spot 交易**, **关闭提现**(Withdraw = OFF)。
- [ ] REQUIRED: **关闭资金转移/内部转账**(Transfer = OFF); 不启用 Futures / Margin / Fiat。
- [ ] REQUIRED: 未开启任何「允许程序化提币」; IP 白名单已配置(仅部署机公网 IP)。

> 只有核对通过后才置 `MAINNET_API_SCOPE_CONFIRM=true`。**若权限无法确认 → BLOCKED,
> 不得启动真实交易**(§8)。此步是「无法用代码自证」的关键人工闸门。

## 3. 首次只读接管(§10, 首次上线)

- [ ] 首次启动会打印「主网首次接管完成」, 核对其 `reconciliation`:
  - `api_ok=true`(账户快照成功);
  - `open_order_count=0`(无意外挂单, 有则 BLOCKED 冻结);
  - `baseline_recorded=true`(HODL 基线已冻结; 既有 SOL 视为初始持仓, 不机械清仓)。
- [ ] REQUIRED: 接管 `allowed=true`; 若 `allowed=false` → 日志打印「主网首次接管未通过, 已冻结交易」,
  **人工复核 blocked_reasons 后再排查, 不强行 bypass**。

## 4. 启动 + 只读观察(§10 / §32)

```bash
docker compose up -d
docker compose logs -f | head -100    # 确认无「拒绝主网启动」「主网就绪自检未通过」「主网首次接管未通过」
curl http://<Pi局域网IP>:8800/api/metrics
```

- [ ] REQUIRED: `=== MAINNET READINESS ===` 全绿(无 blocked_reasons)。
- [ ] REQUIRED: `/api/metrics` 的 `kill_switch.armed=false`、`health.can_buy` 有明确布尔值。
- [ ] 建议: 首次上线**观察 ≥ 24h 不下单**(保持策略阈值极高或资金额度 0), 确认行情/对账/
  健康快照稳定后, 再放开极小资金(见 mainnet-runbook §5 资金递增纪律)。

## 5. go/no-go 结论

```text
[日期] 主网启动前检查
- §0 备份/完整性: PASSED / FAILED
- §1 配置: PAPER_TRADING=false BINANCE_TESTNET=false LIVE_TRADING_CONFIRM=true MAINNET_API_SCOPE_CONFIRM=true
- §2 API 权限(人工): Spot only + 关提现 + 关转账 = 确认
- §3 首次只读接管: allowed=true baseline_recorded=true
- 结论: GO / NO-GO
```

**任一 REQUIRED 项 FAILED → NO-GO。**
