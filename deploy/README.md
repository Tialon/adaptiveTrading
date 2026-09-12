# deploy/ — 部署资产

> 这里放**部署过程用到的零散文件**。真正的部署手册在 [`docs/`](../docs/)，本页只说明目录里有什么、什么时候用。

## 目录内容

| 文件 | 用途 | 什么时候用 |
|------|------|-----------|
| `init.sql` | 建库语句（**仅 `CREATE DATABASE`**，不写表 DDL） | 需要 MySQL 的存量/开发环境。生产走 SQLite，**不需要**它 |
| `pi/production.env.example` | Pi 生产配置模板 | 首次在 Pi 上部署时复制到 `/etc/adaptive-trading/production.env` 再编辑 |

## ⚠️ 为什么不把 Dockerfile / docker-compose.yml 也挪进来

它们**留在仓库根目录**，是刻意的：

`docker-compose.yml` 的持久化卷用的是**相对路径**：

```yaml
volumes:
  - ${HOST_DATA_DIR:-./data}:/app/data
```

相对路径是**相对于 compose 文件所在目录**解析的。一旦把 compose 挪进 `deploy/`，
`./data` 就会变成 `deploy/data` —— 已经在 Pi 上跑着的生产容器会挂到一个**空目录**上，
表现为「数据不见了」。

要挪就得同步把 4 个卷默认值改成 `../data` 之类，并更新 Pi 手册与所有部署命令。
那属于**碰生产部署**的改动，应当单独一轮做，不与文档整理混在一起。

## 相关文档

| 文档 | 内容 |
|------|------|
| [`docs/docker-deployment.md`](../docs/docker-deployment.md) | Docker 构建 / 启动 / 备份 / 升级回滚 / 镜像站覆盖 |
| [`docs/raspberry-pi-deployment.md`](../docs/raspberry-pi-deployment.md) | Pi arm64 无人值守部署 |
| [`docs/mainnet-runbook.md`](../docs/mainnet-runbook.md) | 主网极小资金运行纪律 |
| [`docs/mainnet-prestart-checklist.md`](../docs/mainnet-prestart-checklist.md) | 每次主网启动前的 go/no-go 清单 |
