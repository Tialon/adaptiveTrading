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
- 每个迁移文件的 SHA-256 checksum 随版本一并落库; 重复调用时校验已应用版本的 checksum,
  若「已应用迁移被事后篡改」则硬失败(不静默继续)。
- **V13 起**: 新增**列**走 `_ADDITIVE_COLUMNS` 声明 + `_ensure_additive_columns()` 幂等补列
  (先查 PRAGMA/information_schema 再加), 与迁移文件在同一事务内执行。原因见该常量处的注释 ——
  简单说: `create_all` 只对新**表**建列, 而裸 ALTER 在两个库上都不幂等, 原生 SQL 表达不出
  「有则跳过」。新**表**仍由 `create_all` 负责, 无需写在这里。

诚实边界(原型, 非生产级迁移框架):
- 仅支持**前向 DDL**; 不自动生成迁移脚本(手写 SQL)、无回滚(down)、无 ORM 元数据 diff、
  不重写 `create_all`(ORM 基线仍由 `create_all` 建立, `001_baseline.sql` 仅作版本锚点)。
- 迁移 SQL 按 `;` 粗拆, 不做字符串字面量内的分号/注释转义 —— 手写迁移须用单行 `--` 注释,
  且避免在字符串字面量里裸写 `;`。
- 迁移文件版本号必须唯一; 同版本重复文件会导致唯一键冲突(视为配置错误, 不静默去重)。
- 结构变更仍须同步 docs/database-migration.md + `SCHEMA_VERSION` + test_v129 全量列清单锚点
  (迁移框架只解决「存量库如何执行 DDL」, 不替代「schema 漂移防呆」)。
"""

import asyncio
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from at01_common.logger import get_logger

logger = get_logger("Migration")

# 迁移脚本目录(仓库根 migrations/)
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

# V11.7 P1-2: 进程内迁移互斥(单进程 asyncio)。防止同一事件循环里两个 `upgrade_schema`
# 并发交错 → 双读「未应用」→ 双应用/唯一键冲突。锁串行化后, 后者重读已应用集合而跳过。
# 跨进程(多实例)串行化不在单进程产品边界内, 由 `version` 主键唯一约束兜底(冲突即配置错误)。
# asyncio.Lock 绑定到创建它的首个事件循环(pytest 每测一新循环), 故按 running loop 惰性取锁。
_migration_lock: asyncio.Lock | None = None
_migration_lock_loop = None


def _get_migration_lock() -> asyncio.Lock:
    """返回当前事件循环对应的迁移锁(跨测试循环安全; 生产单进程单循环即单个持久锁)。"""
    global _migration_lock, _migration_lock_loop
    loop = asyncio.get_running_loop()
    if _migration_lock is None or _migration_lock_loop is not loop:
        _migration_lock = asyncio.Lock()
        _migration_lock_loop = loop
    return _migration_lock

# schema_version 表 DDL(按方言)。applied_at 用 "%Y-%m-%d %H:%M:%S" 字符串:
# SQLite(TEXT)与 MySQL(DATETIME)均可接受, 避免带时区偏移的 ISO 串在 MySQL 上被拒。
# checksum: 迁移文件内容 SHA-256(64 hex), V11.7 P1-1 用于检测「已应用迁移被事后篡改」。
_SCHEMA_VERSION_DDL = {
    "sqlite": (
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "version TEXT PRIMARY KEY, applied_at TEXT NOT NULL, description TEXT,"
        " checksum TEXT NOT NULL DEFAULT '')"
    ),
    "mysql": (
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "version VARCHAR(64) PRIMARY KEY, applied_at DATETIME NOT NULL,"
        " description VARCHAR(255), checksum VARCHAR(64) NOT NULL DEFAULT '')"
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


def checksum_of(path: Path) -> str:
    """迁移文件内容的 SHA-256(64 hex)。

    对 `read_text(encoding="utf-8")`(通用换行, 归一 \r\n → \n)后的 UTF-8 字节取哈希,
    使跨平台 checkout(git autocrlf)不产生假漂移; 任何实质内容改动都会改变 checksum。
    """
    return hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()


async def _existing_columns(conn: AsyncConnection, dialect: str, table: str) -> set[str]:
    """读取实际库中某张表的列名集合(PRAGMA / information_schema); 无法判定返回空集。"""
    if dialect == "sqlite":
        rows = (await conn.execute(text(f"PRAGMA table_info({table})"))).fetchall()
        return {str(row[1]) for row in rows}
    if dialect == "mysql":
        rows = (
            await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name=:t AND table_schema=DATABASE()"
                ),
                {"t": table},
            )
        ).scalars().all()
        return {str(c) for c in rows}
    return set()


async def _ensure_checksum_column(conn: AsyncConnection, dialect: str) -> None:
    """旧 schema_version 表(V11.6 建, 无 checksum 列)补齐 checksum 列; 已存在则 no-op。

    ALTER 用 VARCHAR(64) DEFAULT ''(SQLite/MySQL 均接受), 既有行补空串(= 无校验记录)。
    """
    names = await _existing_columns(conn, dialect, "schema_version")
    if dialect == "other":
        return
    if "checksum" not in names:
        await conn.execute(
            text("ALTER TABLE schema_version ADD COLUMN checksum VARCHAR(64) DEFAULT ''")
        )


# ---------------------------------------------------------------------------
# 增量列(V13 起)
# ---------------------------------------------------------------------------
#
# `create_all` 只建**缺失的表**, 对既有表**不做 ALTER** —— 给既有表加一列, 新库有、
# 存量生产库没有, 且它是静默的。这正是本框架要解决的问题。
#
# **为什么不写在 migrations/*.sql 里**: 裸 `ALTER TABLE ... ADD COLUMN` 不幂等 ——
# MySQL 8.0 不支持 `ADD COLUMN IF NOT EXISTS`, 而 `create_all` 已在**任何**库上(空库与
# 存量库都会补建缺失表, 但只对**新表**建列)先一步建好该列时会撞「duplicate column」。
# 原生 SQL 无法可移植地表达「有则跳过」, 故增量列在此集中声明, 由 `_ensure_additive_columns`
# 先查后加。它在**与迁移文件相同的事务**内执行, 语义上仍是同一批前向 DDL。
#
# 新增一列时: 在此登记 + 递增 SCHEMA_VERSION + 更新 test_v129 全量列清单锚点 + 登记
# docs/database-migration.md §3(与 migrations/*.sql 的同步要求完全一致)。
_ADDITIVE_COLUMNS: dict[str, dict[str, str]] = {
    # V13: 急停来源 —— 决定「自动恢复」能否介入。DEFAULT 'MANUAL' 是刻意的 fail-closed:
    # 存量库补列后既有行全部按「需要人工解除」处理, 不会因升级而突然获得自动解冻能力。
    "kill_switch_state": {"origin": "VARCHAR(32) NOT NULL DEFAULT 'MANUAL'"},
}


async def _ensure_additive_columns(conn: AsyncConnection, dialect: str) -> list[str]:
    """幂等补齐 `_ADDITIVE_COLUMNS` 里声明的增量列, 返回本次真正新增的 `表.列` 列表。"""
    added: list[str] = []
    for table, columns in _ADDITIVE_COLUMNS.items():
        existing = await _existing_columns(conn, dialect, table)
        if not existing:
            # 表还不存在(理论上 create_all 已建, 这里只作防御) —— 不猜, 跳过。
            continue
        for column, ddl in columns.items():
            if column in existing:
                continue
            await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
            added.append(f"{table}.{column}")
    return added


async def _applied_versions(conn: AsyncConnection) -> dict[str, str]:
    """已应用迁移版本 → checksum(旧行无 checksum 记为空串)。"""
    rows = (
        await conn.execute(text("SELECT version, checksum FROM schema_version"))
    ).fetchall()
    return {str(r[0]): (str(r[1]) if r[1] is not None else "") for r in rows}


async def upgrade_schema(migrations_dir: Path | None = None) -> list[str]:
    """应用未落库的迁移, 返回本次**新应用**的版本号列表(幂等: 已应用版本跳过)。

    在单个事务内建 `schema_version` 表、逐迁移执行 + 记版本; 事务提交后重复调用为 no-op。
    P1-2: 全程持进程内互斥锁, 并发调用串行化(后者重读已应用集合而跳过)。
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
    async with _get_migration_lock():
        async with engine.begin() as conn:
            await conn.execute(text(ddl))
            await _ensure_checksum_column(conn, dialect)
            # V13: 增量列补列(create_all 不做, 存量库靠这里)。与迁移文件同事务。
            await _ensure_additive_columns(conn, dialect)
            done = await _applied_versions(conn)
            for version, path in list_migrations(migrations_dir):
                current = checksum_of(path)
                if version in done:
                    stored = done[version]
                    # P1-1: 已应用迁移内容被事后篡改 → 配置错误, 硬失败(不静默继续)。
                    if stored and stored != current:
                        raise RuntimeError(
                            f"迁移 {version}({path.name})内容已变更: 已应用 checksum "
                            f"{stored} ≠ 当前 {current}"
                        )
                    if not stored:
                        logger.warning("已应用迁移无 checksum 记录, 无法校验是否被篡改", version=version)
                    continue
                sql = path.read_text(encoding="utf-8")
                for stmt in _split_statements(sql):
                    await conn.execute(text(stmt))
                await conn.execute(
                    text(
                        "INSERT INTO schema_version (version, applied_at, description, checksum) "
                        "VALUES (:version, :applied_at, :description, :checksum)"
                    ),
                    {
                        "version": version,
                        "applied_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                        "description": path.name,
                        "checksum": current,
                    },
                )
                applied.append(version)
                logger.info("应用数据库迁移", version=version, file=path.name, checksum=current[:8])
    return applied
