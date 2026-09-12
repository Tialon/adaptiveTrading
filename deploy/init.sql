-- adaptiveTrading 数据库初始化(仅建库)
--
-- 表结构统一由 ORM `create_all` 负责(run.py 启动时 init_db -> create_all), 本文件不再手写 DDL,
-- 避免手写表结构与 at01_common/models.py 漂移。历史教训:
--   - orders 表缺 reduce_only / accounting_state(新库缺列导致记账态判定失效)
--   - `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` 是 MariaDB 专用语法, MySQL 8 直接报错 1064
--
-- 参考:
--   - 29 张表定义: at01_common/models.py(权威来源; 附录见 docs/architecture.md)
--   - 迁移说明: docs/database-migration.md
--   - 存量库升级: create_all 只建新表、不改旧列; 增量列由
--     at01_common/migrations.py::_ADDITIVE_COLUMNS 幂等补齐(见 database-migration.md §4.1)
--
-- 注(V14 §7 更正): 此前这里写着「生产实际使用 SQLite」—— 那是**旧口径, 已作废**。
--     现在 Docker(Level 2)与 Pi(Level 3)**都跑 MySQL**, 只有 Level 1 本机开发用 SQLite。
--     本文件由 docker-compose.yml 挂到 /docker-entrypoint-initdb.d/, 在 MySQL 首次
--     初始化(空数据目录)时执行一次; 库名/用户也可由 compose 的 MYSQL_* 环境变量直接创建。

CREATE DATABASE IF NOT EXISTS adaptive_trading
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_unicode_ci;

USE adaptive_trading;
