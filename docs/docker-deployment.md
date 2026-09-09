# Docker 部署手册

> V11.8 §3-§5 交付。本文是「用 Docker 把 adaptiveTrading 跑成生产容器」的单一入口。
> 面向 x86_64(amd64)主机; 树莓派(arm64)专有步骤见 [raspberry-pi-deployment.md](raspberry-pi-deployment.md)。
> 测试网验证见 [testnet-runbook.md](testnet-runbook.md); 主网上线见 [mainnet-runbook.md](mainnet-runbook.md)。

## 1. 前置条件

- Docker 20.10+(含 Docker Compose v2); 本仓库源码(`git clone` 或已解包)。
- 可访问 `docker.io`(拉基础镜像)与 `pypi.org`(装依赖)。**网络受限环境**见 §8 镜像站覆盖。

## 2. 配置外置环境文件

开发机可使用仓库内 `.env`：

```bash
cp .env.example .env
```

Pi/生产环境必须把配置放在仓库外（例如 `/etc/adaptive-trading/production.env`），以便升级源码或重建容器时不接触密钥和运行参数：

```bash
sudo install -d -m 700 /etc/adaptive-trading
sudo install -m 600 deploy/pi/production.env.example /etc/adaptive-trading/production.env
sudoedit /etc/adaptive-trading/production.env
docker compose --env-file /etc/adaptive-trading/production.env config
docker compose --env-file /etc/adaptive-trading/production.env up -d --build
```

外置文件必须包含 `ADAPTIVE_TRADING_ENV_FILE=/etc/adaptive-trading/production.env`；compose 据此把它注入容器，同时读取其中的镜像 tag 和宿主机持久化目录。详见 [`deploy/pi/production.env.example`](../deploy/pi/production.env.example)。生产配置文件绝不提交。

按目标模式编辑 `.env`(至少填你需要的部分):

- **纸面 + 测试网(默认、最安全)**: 保持 `PAPER_TRADING=true` + `BINANCE_TESTNET=true`, 无需 key。
- **测试网真实下单**: `PAPER_TRADING=false` + `BINANCE_TESTNET=true` + 测试网 key/secret +
  `RUN_TESTNET_TRADING=1`(三重闸门, 见 [testnet-runbook.md](testnet-runbook.md))。
- **主网**(极谨慎, 见 [mainnet-runbook.md](mainnet-runbook.md)): `BINANCE_TESTNET=false` +
  `LIVE_TRADING_CONFIRM=true` + `MAINNET_API_SCOPE_CONFIRM=true` + 主网 key。

> **密钥安全**: `.env` 已 gitignore, **绝不提交/发布/打进镜像**。`.dockerignore` 兜底排除 `.env`、
> `*.secret`、`secrets/`、`*.pem`、`*.key`。

## 3. 构建镜像

```bash
# 生产镜像请用 git_sha 打 tag(可追溯), 勿用裸 latest
docker build -t adaptive-trading:$(git rev-parse --short HEAD) .
```

构建参数(可选, 网络受限时用):

| ARG | 默认 | 用途 |
|-----|------|------|
| `PYTHON_BASE` | `python:3.13-slim` | 基础镜像(可指镜像站) |
| `PIP_INDEX_URL` | `https://pypi.org/simple` | pip 装 uv 的源 |
| `UV_DEFAULT_INDEX` | `https://pypi.org/simple` | uv 装依赖的源 |

## 4. 启动 / 停止 / 重启

```bash
docker compose up -d          # 构建(如未构建)并后台启动
docker compose ps             # 状态(含 health)
docker compose logs -f        # 日志(JSON structlog)
docker compose restart        # 重启(restart: unless-stopped, 主机/守护重启自动拉起)
docker compose down           # 停止(不删卷, 数据保留在 ./data ./logs ./evidence)
```

## 5. 状态与健康检查

```bash
curl http://localhost:8800/api/health     # {"status":"ok","running":true}
curl http://localhost:8800/api/metrics    # 运行时健康快照(state/can_buy/各维健康)
docker inspect --format '{{.State.Health.Status}}' adaptive-trading
```

> **重要**: `health=OK` 仅表示「HTTP 服务可达」, **不等于可买**。交易许可由 `TradingGate` 单一
> 权威判定 —— 看 `/api/metrics` 的 `health.can_buy` / `health.can_sell` 与 `buy_block_reason`。

## 6. 数据持久化

| 宿主机目录 | 容器路径 | 内容 |
|-----------|---------|------|
| `HOST_DATA_DIR`（默认 `./data`） | `/app/data` | SQLite `adaptive.db`(容器内 `DATABASE_URL=sqlite+aiosqlite:////app/data/adaptive.db`) |
| `HOST_LOGS_DIR`（默认 `./logs`） | `/app/logs` | `adaptive.log`(JSON)+ `soak/<run_id>/` 证据 |
| `HOST_EVIDENCE_DIR`（默认 `./evidence`） | `/app/evidence` | 证据输出 |
| `HOST_REPORTS_DIR`（默认 `./reports`） | `/app/reports` | 每日复盘报告 |

> 三个目录为**持久化卷**(非 tmpfs), `docker compose down` 后仍保留。备份见 §7。

## 7. 备份与恢复(SQLite)

```bash
# 在线一致性备份（WAL 安全；以外置配置中的 HOST_DATA_DIR 为准）
python scripts/db_backup.py --db /srv/adaptive-trading/data/adaptive.db \
  --backup-dir /srv/adaptive-trading/data/backups --keep 30
```

恢复必须先停止容器，在**副本目录**验证备份完整性后才由操作者执行替换；不得直接覆盖正在运行的 DB。

## 8. 网络受限环境(镜像站覆盖)

docker.io / pypi.org 不可达时(如国内网络), 用构建参数指向镜像站:

```bash
docker build \
  --build-arg PYTHON_BASE=docker.1ms.run/library/python:3.13-slim \
  --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  --build-arg UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple \
  -t adaptive-trading:$(git rev-parse --short HEAD) .
```

compose 下也可在 `docker-compose.yml` 的 `build.args` 传入同样参数(或 `docker compose build --build-arg ...`)。

## 9. 升级与回滚

镜像 tag 绑定 git_sha, 升级 = 拉到新代码 → 构建新 tag → `docker compose up -d` 切换; 回滚 =
切回旧 tag:

```bash
# 升级
git pull
docker compose up -d --build

# 回滚到旧镜像 tag(需旧 tag 仍在本地/仓库)
docker tag adaptive-trading:旧sha adaptive-trading:local
docker compose up -d
```

> 每次发布都有独立 tag(如 `adaptive-trading:91797a0`), 不覆盖、可追溯、可回滚。

## 10. 优雅停机 / 故障恢复

- 容器 `STOPSIGNAL SIGTERM` + `tini` PID 1: `docker stop` 会先发 SIGTERM, `run.py` 优雅停机
  (禁新开仓 → 回收后台任务 → flush 风控事件 → 关 DB)。
- `restart: unless-stopped`: 进程崩溃/主机重启后自动拉起; **但急停冻结态(kill_switch)持久化**,
  重启后仍保持冻结, 不自动复位(需人工 `recover`)。
- 容器内 DB 故障/数据目录只读 → 应用 fail-fast 或降级, 由对账/急停兜底; 详见 [runbook.md](runbook.md)。

## 11. 已知边界

- 本手册验证过 **amd64**; arm64(Pi)见 [raspberry-pi-deployment.md](raspberry-pi-deployment.md)。
- 单容器单进程(asyncio), 不引入 K8s/微服务/编排平台(产品冻结)。
- HEALTHCHECK 只判 HTTP 可达, 交易许可以 `/api/metrics` 的 `health.can_buy` 为准。
