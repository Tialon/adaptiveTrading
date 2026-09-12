"""V11.6 P1-4 证明: 最小数据库迁移框架 —— 前向 DDL 幂等应用 + 版本簿记 + 方言检测。

覆盖(与 at01_common/migrations.py 的 docstring 契约对齐):
1. `detect_dialect`: DATABASE_URL → sqlite / mysql / other;
2. `list_migrations`: 按 `NNN_` 版本号升序、忽略无版本号前缀文件、目录缺失返回空;
3. `_split_statements`: 按 `;` 粗拆、去单行注释与空行;
4. `upgrade_schema`: 应用未落库迁移并记 schema_version; 重复调用幂等(不重复执行);
5. 真实基线 `migrations/001_baseline.sql` 记为已应用版本;
6. `init_db` 建表后 schema_version 作为内部表不产生 schema 漂移(check_schema 无漂移)。
"""

from pathlib import Path

from sqlalchemy import text

from at01_common import schema_check
from at01_common.database import get_engine
from at01_common.migrations import (
    _split_statements,
    detect_dialect,
    list_migrations,
    upgrade_schema,
)


# ---- 纯函数: 方言检测 / 迁移列举 / SQL 拆分 ----

def test_detect_dialect():
    assert detect_dialect("sqlite+aiosqlite:///./adaptive.db") == "sqlite"
    assert detect_dialect("mysql+aiomysql://u:p@h/db") == "mysql"
    assert detect_dialect("postgresql+asyncpg://h/db") == "other"
    assert detect_dialect("") == "other"


def test_list_migrations_sorted_and_filtered(tmp_path):
    (tmp_path / "001_a.sql").write_text("x", encoding="utf-8")
    (tmp_path / "003_c.sql").write_text("x", encoding="utf-8")
    (tmp_path / "002_b.sql").write_text("x", encoding="utf-8")
    (tmp_path / "README.sql").write_text("x", encoding="utf-8")  # 无版本号前缀 → 忽略

    got = list_migrations(tmp_path)
    assert [v for v, _ in got] == ["001", "002", "003"]
    assert [p.name for _, p in got] == ["001_a.sql", "002_b.sql", "003_c.sql"]


def test_list_migrations_missing_dir_returns_empty():
    assert list_migrations(Path("/nonexistent-dir-xyz")) == []


def test_split_statements_strips_comments_and_empty():
    sql = (
        "-- 注释行\n"
        "CREATE TABLE t1 (id INTEGER);\n"
        "\n"
        "  \n"
        "CREATE INDEX ix ON t1(id);\n"
    )
    assert _split_statements(sql) == [
        "CREATE TABLE t1 (id INTEGER)",
        "CREATE INDEX ix ON t1(id)",
    ]


# ---- 核心: 幂等应用 + 版本簿记 ----

async def _table_names() -> set[str]:
    engine = get_engine()
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        ).scalars().all()
    return set(rows)


async def test_upgrade_schema_applies_and_is_idempotent(tmp_path):
    (tmp_path / "001_t1.sql").write_text(
        "CREATE TABLE IF NOT EXISTS t1 (id INTEGER PRIMARY KEY);", encoding="utf-8"
    )
    (tmp_path / "002_t2.sql").write_text(
        "CREATE TABLE IF NOT EXISTS t2 (id INTEGER PRIMARY KEY);", encoding="utf-8"
    )

    first = await upgrade_schema(tmp_path)
    assert first == ["001", "002"]
    assert {"t1", "t2", "schema_version"} <= await _table_names()

    # 幂等: 再次调用不重复应用、不报错
    assert await upgrade_schema(tmp_path) == []
    assert {"t1", "t2", "schema_version"} <= await _table_names()


async def test_baseline_migration_recorded_and_idempotent():
    """真实 migrations/*.sql 全部记为已应用版本; 重复调用幂等。

    V13 起不再钉死 `["001"]` 这个字面量 —— 每加一个迁移都要改测试的话, 这行迟早会被
    顺手改成「当时的值」而不是「正确的行为」。改为断言**行为**: 首次应用 = 磁盘上的全部
    迁移文件, 再次应用 = 空, 落库版本集合 = 磁盘文件集合。
    """
    on_disk = {version for version, _ in list_migrations()}
    assert "001" in on_disk  # 基线锚点必须仍在

    assert set(await upgrade_schema()) == on_disk
    assert await upgrade_schema() == []

    engine = get_engine()
    async with engine.connect() as conn:
        versions = (
            await conn.execute(text("SELECT version FROM schema_version"))
        ).scalars().all()
    assert set(versions) == on_disk


async def test_init_db_creates_schema_version_and_no_schema_drift(db_tables):
    """init_db 建表 + 迁移后: schema_version 为内部表, 不产生 schema 漂移。"""
    assert "schema_version" in await _table_names()

    drift = await schema_check.check_schema()
    assert drift == [], schema_check.format_drift(drift)
