# 树莓派(Raspberry Pi)生产部署

> V11.8 §13-§20 交付。本文是「把 adaptiveTrading 部署到树莓派(arm64)长期无人值守运行」的
> 专有步骤; 通用 Docker 操作见 [docker-deployment.md](docker-deployment.md)。
> **诚实边界**: 本轮在 amd64 主机上验证了镜像构建 + 容器冒烟; **真实 Pi(arm64)构建尚未执行**
> (本机无 ARM64 宿主), 以下为已按多架构设计的就绪步骤, 待部署环境实测后回填。

## 1. 为什么是树莓派

- 低功耗(约 5W)、静音、可 7×24 挂机, 契合「单币低频摆动交易」对算力/功耗的低要求。
- 单机 SQLite(SQLite WAL/busy_timeout 已加固, 见 [database-migration.md](database-migration.md))
  零 MySQL/Redis 依赖, 减少 Pi 上的运维面。
- Docker 提供可复现运行时 + 依赖隔离 + `restart: unless-stopped` 自动拉起。

## 2. 前置条件

- Raspberry Pi 4/5(建议 ≥ 4GB RAM), 官方 Raspberry Pi OS(64-bit, arm64)或 Ubuntu Server arm64。
- Docker + Docker Compose v2(装法见官方文档, 或用 `get.docker.com` 脚本)。
- 网络可访问 `docker.io` + `pypi.org`(受限则见 [docker-deployment.md](docker-deployment.md) §8)。

## 3. 准备代码与 .env

```bash
git clone <repo-url> adaptiveTrading && cd adaptiveTrading
git checkout main
cp .env.example .env
# 编辑 .env: 至少设 SYMBOLS=SOLUSDT、目标模式(纸面/测试网)、以及 TZ
```

> Pi 上时钟/时区: 设 `TZ=UTC`(容器内已 `ENV TZ=UTC`), 或用 `TZ=Asia/Shanghai` 按需;
> 宿主 `timedatectl` 建议同步 NTP(交易时间戳依赖)。

## 4. 构建 arm64 镜像

在 Pi 上直接构建(无需跨架构):

```bash
docker build -t adaptive-trading:$(git rev-parse --short HEAD) .
```

基础镜像 `python:3.13-slim` 自动解析为 arm64 变体。**网络受限时**用镜像站覆盖:

```bash
docker build \
  --build-arg PYTHON_BASE=docker.1ms.run/library/python:3.13-slim \
  --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  --build-arg UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple \
  -t adaptive-trading:$(git rev-parse --short HEAD) .
```

> 若要在 amd64 开发机交叉构建再推到 Pi, 用 `docker buildx build --platform linux/arm64 ...`
> 并 `docker save`/registry 中转(需 buildx 与 registry 可达; 本会话未执行)。

## 5. 启动与自启

```bash
docker compose up -d
docker compose ps                 # 确认 health 为 healthy
```

`restart: unless-stopped` 已保证进程/主机重启后自动拉起。Pi 断电重启后, Docker daemon 起来
会自动带起容器。

## 6. 无人值守自检清单

上线无人值守前, 在 Pi 上逐项确认:

1. `docker compose ps` health = healthy, 且 `curl localhost:8800/api/metrics` 的 `state` 为
   `TRADING`(纸面)或按测试网闸门进入的预期态。
2. `curl localhost:8800/api/health` 返回 200。
3. **断电重启演练**: 拔电 → 上电 → 等 Docker 自启 → 确认容器自动拉起、急停/持仓状态正确恢复
   (急停冻结态重启后**仍冻结**, 不自动复位, 这是设计行为)。
4. 日志目录 `./logs` 与 DB `./data` 持续写入(Pi SD 卡建议换工业级/高耐久卡或 SSD, 减少写磨损)。
5. `df -h` 确认数据盘不撑满(长期运行日志会增长, 轮转策略见 [runbook.md](runbook.md))。

## 7. 树莓派特有注意事项

- **SD 卡写入寿命**: SQLite WAL + 日志频繁写会磨损 SD 卡; 建议用 SSD(USB)挂 `/app/data`/`/app/logs`,
  或至少用高耐久 SD 卡并定期备份。
- **散热**: 长期 100% CPU(WS 消息 + 策略循环)需散热片/风扇; 观察 `vcgencmd measure_temp`。
- **电源**: 用稳定 5V/3A 电源, 电压不稳易导致重启; 重启后靠 `restart: unless-stopped` 自愈。
- **时区/NTP**: 交易时间戳、K 线对齐依赖时钟, 务必 NTP 同步。

## 8. 诚实边界(截至 V11.8)

- **arm64 真实构建/运行未执行**: 本会话无 ARM64 宿主, 未在真实 Pi 上跑过; 上述为设计就绪步骤。
- **测试网 7h/24h soak 未执行**: L3 未达, 详见 [testnet-operation.md](testnet-operation.md)。
- 真正在 Pi 上无人值守运行 + soak 通过后, 回填本文件的「实测」结论。
