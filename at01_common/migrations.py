"""最小数据库迁移框架(V11.6 P1-4)

现状(审查结论): 项目无 Alembic; `init_db` 用 `Base.metadata.create_all` 只建**缺失**表、
不对既有表做 ALTER(加列/改列/索引都不传播到已存在的生产库)。此前的升级靠 runbook 手动
ALTER + 同步三处锚点(见 docs/database-migration.md)。

本模块补一个**最小可用的前向 DDL 迁移**机制:
- `schema_version` 表: 迁移框架自身的簿记(记录已应用版本号), 是原始 SQL 建的内部表,
  **不是 ORM 领域表**(不进入 `Base.metadata`, 不参与 test_v129 表清单锚点), 亦被
  `schema_check` 视为内部表过滤(与 `alembic_version` 同列)。
- `migrations/*.sql`: 编号迁移文件, 命名 `NNN_描述.sql`, 手写 DDL, 按版本号升序应用;
- `upgrade_schema()`: 检测 SQLite/MySQL 方言, 只应用未落库的迁移并记录版本; 重复调用
  幂等(已应用版本跳过)。

诚实边界(原型, 非生产级迁移框架):
- 仅支持**前向 DDL**; 不自动生成迁移脚本(手写 SQL)、无回滚(down)、无 ORM 元数据 diff、
  不重写 `create_all`(ORM 基线仍由 `create_all` 建立, `001_baseline.sql` 仅作版本锚点)。
- 迁移 SQL 按 `;` 粗拆, 不做字符串字面量内的分号/注释转义 —— 手写迁移须用单行 `--` 注释,
  且避免在字符串字面量里裸写 `;`。
- 迁移文件版本号必须唯一; 同版本重复文件会导致唯一键冲突(视为配置错误, 不静默去重)。
- 结构变更仍须同步 docs/database-migration.md + `SCHEMA_VERSION` + test_v129 全量列清单锚点
  (迁移框架只解决「存量库如何执行 DDL」, 不替代「schema 漂移防呆」)。
"""

import re
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from at01_common.logger import get_logger

logger = get_logger("Migration")

# 迁移脚本目录(仓库根 migrations/)
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

# schema_version 表 DDL(按方言)。applied_at 用 "%Y-%m-%d %H:%M:%S" 字符串:
# SQLite(TEXT)与 MySQL(DATETIME)均可接受, 避免带时区偏移的 ISO 串在 MySQL 上被拒。
_SCHEMA_VERSION_DDL = {
    "sqlite": (
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "version TEXT PRIMARY KEY, applied_at TEXT NOT NULL, description TEXT)"
    ),
    "mysql": (
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "version VARCHAR(64) PRIMARY KEY, applied_at DATETIME NOT NULL,"
        " description VARCHAR(255))"
    ),
}


def detect_dialect(database_url: str) -> str:
    """从 DATABASE_URL 判断方言: "sqlite" / "mysql" / "other"(纯字符串判断)。"""
    url = (database_url or "").lower()
    if url.startswith("sqlite"):
        return "sqlite"
    if url.startswith("mysql"):
        return "mysql"
    return "other"


def list_migrations(migrations_dir: Path | None = None) -> list[tuple[str, Path]]:
    """扫描 `migrations/*.sql`, 解析 `NNN_` 版本号前缀, 按版本号升序返回 [(version, path), ...]。

    无版本号前缀的文件(如 README.sql)被忽略。
    """
    directory = migrations_dir or MIGRATIONS_DIR
    if not directory.is_dir():
        return []
    entries: list[tuple[int, str, Path]] = []
    for p in directory.glob("*.sql"):
        m = re.match(r"^(\d+)_", p.name)
        if m:
            entries.append((int(m.group(1)), m.group(1), p))
    entries.sort(key=lambda x: x[0])
    return [(version, path) for _, version, path in entries]


def _split_statements(sql: str) -> list[str]:
    """把迁移 SQL 拆成单条语句: 按 `;` 粗拆, 去掉单行 `--` 注释与空行。

    原型边界: 不做字符串字面量内分号/注释转义 —— 手写迁移须避免在字符串里裸写 `;`。
    """
    statements: list[str] = []
    for raw in sql.split(";"):
        cleaned = "\n".join(
            line
            for line in raw.splitlines()
            if line.strip() and not line.strip().startswith("--")
        )
        if cleaned.strip():
            statements.append(cleaned.strip())
    return statements


async def _applied_versions(conn: AsyncConnection) -> set[str]:
    rows = (await conn.execute(text("SELECT version FROM schema_version"))).scalars().all()
    return set(str(v) for v in rows)


async def upgrade_schema(migrations_dir: Path | None = None) -> list[str]:
    """应用未落库的迁移, 返回本次**新应用**的版本号列表(幂等: 已应用版本跳过)。

    在单个事务内建 `schema_version` 表、逐迁移执行 + 记版本; 事务提交后重复调用为 no-op。
    """
    from at01_common.database import get_engine
    from at01_common.settings import get_settings

    engine = get_engine()
    dialect = detect_dialect(get_settings().database_url)
    ddl = _SCHEMA_VERSION_DDL.get(dialect)
    if ddl is None:
        logger.warning("未识别的数据库方言, 跳过迁移框架(仅支持 sqlite/mysql)", dialect=dialect)
        return []

    applied: list[str] = []
    async with engine.begin() as conn:
        await conn.execute(text(ddl))
        done = await _applied_versions(conn)
        for version, path in list_migrations(migrations_dir):
            if version in done:
                continue
            sql = path.read_text(encoding="utf-8")
            for stmt in _split_statements(sql):
                await conn.execute(text(stmt))
            await conn.execute(
                text(
                    "INSERT INTO schema_version (version, applied_at, description) "
                    "VALUES (:version, :applied_at, :description)"
                ),
                {
                    "version": version,
                    "applied_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    "description": path.name,
                },
            )
            applied.append(version)
            logger.info("应用数据库迁移", version=version, file=path.name)
    return applied
