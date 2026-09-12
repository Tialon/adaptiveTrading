# CC 执行任务 — Pi 生产就绪与小资金运行前置（2026-09-09）

> 目标：让 Raspberry Pi 4/5（arm64）具备可审计、可恢复、局域网可管理的生产运行条件。
> 本任务**不授权主网下单、不授权资金划转、不授权扩大币种或策略**。所有主网门槛完成前，保持 `PAPER_TRADING=true`、`BINANCE_TESTNET=true`。

## P0：先清除当前工程阻塞项

按 [cc_task_v12_1.md](cc_task_v12_1.md) 完成以下验收后才进入部署：

- `uv run ruff check .` 为 0 errors；
- `uv run mypy` 为 0 errors（不得用 ignore/cast 掩盖 Optional REST、订单 ID、时间和数值问题）；
- `uv run pytest -q --cov --cov-report=term-missing --cov-fail-under=75 -m "not testnet"` 全绿且 coverage ≥ 75%；
- 在 `docs/progress.md` 写明实际命令、精确测试/coverage、commit SHA。提交并推送后再部署。

## P1：Pi 与外置配置准备（不接真实资金）

1. 使用 64-bit Raspberry Pi OS/Ubuntu、Pi 4/5 ≥4GB、稳定电源、散热和 **SSD**；不得把长期 SQLite/WAL/日志放在普通 SD 卡。
2. 安装 Docker Engine + Compose v2，启用 Docker 自启动；确认 `docker version`、`docker compose version`、`uname -m` 为 `aarch64`。
3. 同步 NTP，核查时区、磁盘空间与温度；建立 `/srv/adaptive-trading/{data,logs,evidence,reports}`。
4. 将 `deploy/pi/production.env.example` 复制到 `/etc/adaptive-trading/production.env`，权限设为 `0600`；填写实际 Git SHA、强 `WEB_ADMIN_TOKEN`、SSD 绝对路径。该文件不得提交、不得复制到镜像或聊天记录。
5. 用外置文件启动并验证渲染结果：

```bash
docker compose --env-file /etc/adaptive-trading/production.env config
docker compose --env-file /etc/adaptive-trading/production.env up -d --build
docker compose ps
curl -fsS http://127.0.0.1:8800/api/health
curl -fsS http://127.0.0.1:8800/api/metrics
```

验收：容器 healthy；数据、日志、证据、日报均写入 SSD 外置目录；`health.can_buy` 的状态和阻断原因可读；空/错误令牌不能调用写接口，正确令牌可调用已授权管理接口。

## P2：局域网与持久化演练

1. 防火墙仅允许受信任局域网网段访问 TCP 8800；禁止路由器端口映射、UPnP 和公网暴露。至少从另一台 LAN 设备验证 Dashboard 可访问、未带 token 的写接口被拒。
2. 执行在线备份：`python scripts/db_backup.py --db /srv/adaptive-trading/data/adaptive.db --backup-dir /srv/adaptive-trading/data/backups --keep 30`；验证 `integrity_check OK`、备份文件可读。
3. 为每日备份建立宿主机 systemd timer（调用上述脚本，日志落 `/srv/adaptive-trading/logs`）；用 `systemctl list-timers` 证明已调度。备份留存周期、异地副本和恢复责任人须记录。
4. 演练：`docker compose stop` / start、Docker daemon 重启、Pi 重启。每次确认容器自动恢复、SQLite 完整、kill switch 冻结态不自动解除、无意外 BUY。
5. 做一次从备份到独立临时目录的恢复演练；原始生产数据库不可直接覆盖，验收为恢复库完整性检查通过。

## P3：arm64 与测试网运行证据

1. 在 Pi 原生构建 arm64 镜像，记录 `IMAGE_TAG`/`GIT_SHA`、`docker image inspect` 架构与容器健康结果。
2. 纸面模式连续运行 ≥24h，保留 health、日志、日报与 DB 备份证据；期间必须无未解释异常、无磁盘/温度告警。
3. 通过人工审批后，按 `testnet-runbook.md` 启用测试网真实执行；依次完成真实订单闭环、7h soak、24h soak。每一步记录 run_id、git SHA、配置摘要、结果和失败处理；未通过不得跳过。
4. 测试网阶段必须复测：断网/REST 超时、重启恢复、订单未知态、对账漂移、急停与人工 recover。任一异常只允许进入 fail-closed 状态。

## P4：小资金主网的最终 go/no-go（需操作者人工批准）

只有 P0–P3 全部 PASS 后，操作者才能逐项执行 [mainnet-readiness.md](../mainnet-readiness.md) 与 [mainnet-prestart-checklist.md](../mainnet-prestart-checklist.md)。另外必须：

- Binance API key 为 Spot only、关闭提现/资金转移、设 IP 白名单；
- 在外置 env 文件中以审批过的金额填入保守风险上限，禁止沿用模板或默认大额参数；
- 先完成主网首次只读接管与至少 24h 不下单观察；
- 每次启动前完成备份、完整性检查、配置核对与 go/no-go 记录；
- 首笔真实小资金动作须由操作者单独明确批准，且不能由 CC 自行开启。

## 完成记录（CC 必填）

每个 P 阶段完成后，更新 `docs/progress.md` 与本文件：实际命令、时间、Pi 架构、镜像/Git SHA、配置文件路径（不含内容和密钥）、测试/soak run_id、结果、剩余风险。每阶段独立提交并推送 `main`。
**结束前保存进度信息**：确认 `git status --short`，不提交 `/etc` 下配置、`.env`、密钥、数据、日志或 `.claude/`。
