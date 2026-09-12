# 主网运维手册(Mainnet Runbook)

> V11.8 §24 交付。这是「通过主网就绪复审后、在 Binance 主网以极小资金真实交易」的运维手册。
> **前置**: 必须先通过 [mainnet-readiness.md](mainnet-readiness.md) 的人工复审(含 L3 达成),
> 且**当前任务不直接打开主网**。通用启动/排障/API 见 [runbook.md](runbook.md); 本文只写主网特有操作。

## 0. 安全总则(不可妥协)

1. **默认禁主网**: `BINANCE_TESTNET=true` + `PAPER_TRADING=true` 是出厂默认; 任何主网配置都需
   多重显式确认(`LIVE_TRADING_CONFIRM=true` + `MAINNET_API_SCOPE_CONFIRM=true` +
   `BINANCE_TESTNET=false`), 否则启动拦截。
2. **主网 key 隔离**: 主网 key 只出现在部署机 `.env`(gitignore), 与测试网 key 完全分离。
3. **极小资金**: 首期资金以「可承受全部亏损」为上限; 逐期按验证结果决定是否小幅递增(见 §5)。
4. **可追踪可审计**: 每笔订单/成交/持仓/成本/对账/恢复均可由 DB + 证据链追溯。

## 1. 上线前(见 mainnet-readiness.md)

主网配置 `.env`:

```ini
SYMBOLS=SOLUSDT
PAPER_TRADING=false                 # 真实下单
BINANCE_TESTNET=false               # 连主网
BINANCE_API_KEY=...                 # 主网 key(Spot only, 关提现)
BINANCE_API_SECRET=...
LIVE_TRADING_CONFIRM=true           # 主网守卫: 必须显式 true
MAINNET_API_SCOPE_CONFIRM=true      # 主网就绪自检: 人工确认 key 权限后置 true
MAINNET_READINESS_ENABLED=true
DATABASE_URL=sqlite+aiosqlite:////app/data/adaptive.db   # 容器内持久化卷
API_HOST=127.0.0.1                  # 或 0.0.0.0 + 非空 WEB_ADMIN_TOKEN
WEB_ADMIN_TOKEN=<强随机令牌>
AI_ENABLED=false                    # 上线初期关 AI(仅参数建议, 非下单路径)
STARTUP_RECONCILE_ENABLED=true
RECONCILE_INTERVAL_SECONDS=300
EQUITY_RECONCILE_TOLERANCE_PCT=0.02
```

启动会打印 `=== MAINNET READINESS ===` 全绿报告后才会继续; 任一不满足 → BLOCKED。

## 2. 启动与首次确认

```bash
docker compose up -d
docker compose logs -f | head -80   # 确认无「拒绝主网启动」「主网就绪自检未通过」
curl http://localhost:8800/api/metrics   # state / can_buy / kill_switch.armed
```

首次上线建议**观察至少 24h 不下单**(把策略阈值抬高或资金设 0), 确认行情/对账/健康快照稳定后再
放开极小资金。

## 3. 监控(一个词回答「能不能交易」)

看 `/api/metrics` 的 `state` 与 `health.can_buy` / `buy_block_reason`(同 testnet-runbook §5)。

- `KILLED` / `SAFE_MODE` / `RECOVERY` → 立即人工介入, 不要裸 reset(见 §4)。
- `can_buy=false` → 看 `buy_block_reason` 定位(急停/对账/行情/熔断/关键任务/停机)。

## 4. 告警处置(主网特有)

| 现象 | 处置 |
|------|------|
| 启动被拦(BLOCKED) | 逐条看 `=== MAINNET READINESS ===` blocked_reasons, 修正后重启; 不强行 bypass |
| `state=KILLED` | 查对账/熔断差异 → 人工确认收敛 → `POST /api/emergency/recover`(重启不自动复位) |
| 资金漂移告警 | 先 `docker compose stop` 停止一切下单 → 查交易所 vs 本地差异 → 人工收敛 |
| 怀疑 key 泄漏 | 立即在币安吊销 key → 换新 key 改 .env → 重启 |
| 任何「看不懂的差异」 | 宁停勿猜: 急停 + 停机 + 人工查证, **绝不在不确定时继续自动交易** |

## 5. 资金递增纪律

- 只有当前资金级别**连续稳定运行 + 对账恒平衡 + 无 KILLED** 后, 才可小幅递增。
- 递增前重新过 [mainnet-readiness.md](mainnet-readiness.md) 复审。
- **禁止**: 一次性放大资金 / 加杠杆 / 换合约 / 加币种(产品冻结, 见 §8)。

## 6. 停机与恢复

- 优雅停机: `POST /api/shutdown` 或 `docker compose stop`(SIGTERM → 禁开仓 → flush → 关 DB)。
- 急停持久化: 重启后冻结态保留, 需人工 `recover`。
- 恢复: `POST /api/emergency/recover`(需 `X-Admin-Token`), 仅在人工确认差异收敛后执行。

## 7. 审计与追溯

- DB 26 张表(订单/成交/持仓/lot/分配/账本/急停/事件/HODL 基准)+ 证据链(`logs/soak/<run_id>/` 或
  `evidence_chain.py`), 每一笔「谁下的、成交多少、成本几何、对账结果」可还原。
- 每日复盘 `reports/YYYY-MM-DD.md` 自动产出, 附运行状态快照。

## 8. 冻结红线(违反即回滚)

主网上线后仍**禁止**: 扩资金扩币种 / Futures / 杠杆 / HFT / 复杂新指标(RSI/MACD/BB…)/
Transformer/RL / LLM 自动下单 / AI 绕过 TradingGate / 关掉对账或急停 / 忽略财务不变量 /
把「无报错」当「通过」。详见 V11.8 规格 §48 禁止清单。
