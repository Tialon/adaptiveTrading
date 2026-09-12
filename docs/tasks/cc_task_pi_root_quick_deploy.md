# CC 执行任务 — Root 用户快速部署到生产 Pi（内网版，2026-09-11）

> 目标：用 `root` 用户在生产 Raspberry Pi 上快速部署 adaptiveTrading，让服务在本地内网可访问并可随 Pi 重启自动恢复。
>
> 本任务面向“先跑起来”的内网生产部署：不展开密钥申请、密钥权限审计、外网暴露、域名、HTTPS、复杂备份策略。配置文件仍不得提交到仓库。

## 0. 执行边界

- 全程使用 `root` 登录 Pi 执行。
- 服务只在本地内网访问，不做公网端口映射。
- 初始运行建议保持 `PAPER_TRADING=true`；如操作者已明确提供生产交易配置，CC 只负责填入外置 env 并启动，不在聊天或日志中展示配置内容。
- 完成标准是容器 healthy、内网 Dashboard 可访问、数据写入 Pi 持久化目录、重启后自动恢复。

## 1. 上线前代码检查

先在本地开发机或 CI 上执行，确认当前 `main` 可以作为部署版本：

```bash
git status --short
git rev-parse --short HEAD
uv run ruff check .
uv run mypy
uv run pytest -q --cov --cov-report=term-missing --cov-fail-under=75 -m "not testnet"
docker compose --env-file deploy/pi/production.env.example config
```

如果本地无法直接渲染 Pi 模板路径，可临时覆盖 env 文件路径后再检查：

```bash
ADAPTIVE_TRADING_ENV_FILE=deploy/pi/production.env.example docker compose --env-file deploy/pi/production.env.example config
```

验收：

- `git status --short` 中没有未解释的代码改动；不得包含 `.env`、密钥、数据库、日志或证据文件。
- 记录部署 Git SHA，后续 Pi 上拉取到同一个 SHA。
- `ruff`、`mypy`、非 testnet 测试全部通过，coverage ≥ 75%。
- Compose 配置渲染成功；失败则先修复，不进入 Pi 部署。
- 将检查结果写入 `docs/progress.md`，再开始下一步。

## 2. 登录 Pi 并准备目录

```bash
ssh root@<PI_LAN_IP>
mkdir -p /opt/adaptiveTrading
mkdir -p /etc/adaptive-trading
mkdir -p /srv/adaptive-trading/{data,logs,evidence,reports}
chmod 700 /etc/adaptive-trading
```

验收：

- 当前用户是 `root`：`id -u` 输出 `0`。
- `/srv/adaptive-trading` 位于目标持久化磁盘；若没有 SSD，也先记录“当前使用系统盘”。

## 3. 安装 Docker

```bash
apt update
apt install -y ca-certificates curl git
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker
docker version
docker compose version
```

验收：

- `docker version` 正常输出 server 信息。
- `docker compose version` 正常输出 Compose v2。

## 4. 拉取或更新代码

```bash
cd /opt
if [ ! -d adaptiveTrading/.git ]; then
  git clone <REPO_URL> adaptiveTrading
fi
cd /opt/adaptiveTrading
git fetch --all --prune
git checkout main
git pull --ff-only
git rev-parse --short HEAD
```

验收：

- 仓库位于 `/opt/adaptiveTrading`。
- 记录当前 Git SHA，后续 `IMAGE_TAG` 与 `GIT_SHA` 使用同一个值。

## 5. 创建生产 env

```bash
cd /opt/adaptiveTrading
install -m 600 deploy/pi/production.env.example /etc/adaptive-trading/production.env
nano /etc/adaptive-trading/production.env
```

最小修改项：

```bash
ADAPTIVE_TRADING_ENV_FILE=/etc/adaptive-trading/production.env
HOST_DATA_DIR=/srv/adaptive-trading/data
HOST_LOGS_DIR=/srv/adaptive-trading/logs
HOST_EVIDENCE_DIR=/srv/adaptive-trading/evidence
HOST_REPORTS_DIR=/srv/adaptive-trading/reports
IMAGE_TAG=<GIT_SHA>
GIT_SHA=<GIT_SHA>
API_HOST=0.0.0.0
API_PORT=8800
WEB_ADMIN_TOKEN=<由操作者提供或现场生成>
PAPER_TRADING=true
BINANCE_TESTNET=true
```

验收：

- `/etc/adaptive-trading/production.env` 权限为 `600`。
- 不把 env 内容打印到聊天、日志、commit 或截图中。

## 6. 构建并启动

```bash
cd /opt/adaptiveTrading
docker compose --env-file /etc/adaptive-trading/production.env config
docker compose --env-file /etc/adaptive-trading/production.env up -d --build
docker compose --env-file /etc/adaptive-trading/production.env ps
```

验收：

- `docker compose config` 成功。
- 容器状态为 running，health 为 healthy 或正在进入 healthy。
- 镜像 tag 使用 Git SHA，不使用裸 `latest`。

## 7. 本机与内网访问验证

在 Pi 上执行：

```bash
curl -fsS http://127.0.0.1:8800/api/health
curl -fsS http://127.0.0.1:8800/api/metrics
hostname -I
```

在同一内网电脑浏览器访问：

```text
http://<PI_LAN_IP>:8800
```

验收：

- Pi 本机 `/api/health` 返回成功。
- Pi 本机 `/api/metrics` 返回运行状态。
- 同一内网电脑可以打开 Dashboard。
- 不配置路由器端口转发，不暴露公网。

## 8. 重启自恢复验证

```bash
reboot
```

Pi 回来后：

```bash
ssh root@<PI_LAN_IP>
cd /opt/adaptiveTrading
docker compose --env-file /etc/adaptive-trading/production.env ps
curl -fsS http://127.0.0.1:8800/api/health
```

验收：

- Docker 随系统启动。
- adaptive-trading 容器自动恢复。
- health 仍成功。

## 9. 完成回填

在 `docs/progress.md` 回填：

```text
[YYYY-MM-DD HH:mm] Pi root quick deploy
- Pi LAN IP:
- Git SHA:
- Docker version:
- Compose version:
- Env path: /etc/adaptive-trading/production.env
- Data path: /srv/adaptive-trading/data
- Container status:
- Health:
- Metrics:
- Reboot recovery: PASS / FAIL
- Mainnet/live trading status: PAPER / TESTNET / MAINNET
```

最后执行：

```bash
git status --short
```

验收：

- 仓库中不出现 `.env`、生产 env、密钥、数据库、日志、证据文件。
- 若有失败，记录失败命令、错误摘要和下一步。
