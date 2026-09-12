-- adaptiveTrading 数据库初始化(仅建库)
--
-- 表结构统一由 ORM `create_all` 负责(run.py 启动时 init_db -> create_all), 本文件不再手写 DDL,
-- 避免手写表结构与 at01_common/models.py 漂移。历史教训:
--   - orders 表缺 reduce_only / accounting_state(新库缺列导致记账态判定失效)
--   - `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` 是 MariaDB 专用语法, MySQL 8 直接报错 1064
--
-- 参考:
--   - 28 张表定义: at01_common/models.py(权威来源; 附录见 docs/architecture.md)
--   - 迁移说明: docs/runbook.md「数据库迁移」章节
--   - 存量库升级: create_all 只建新表、不改旧列, 需手动 ALTER(见 runbook)
--
-- 注: 生产实际使用 SQLite(见 docker-compose.yml DATABASE_URL), 本文件仅供
--     需要 MySQL 的存量/开发环境建库用。

CREATE DATABASE IF NOT EXISTS adaptive_trading
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_unicode_ci;

USE adaptive_trading;
