"""V13 增量列机制测试 —— 让存量库能被**追溯修复**。

**为什么需要这个机制**(实测踩到的真问题):
`docs/database-migration.md` §3 里 V9.0 / V10.3 / V10.5 / V10.6 的增量列, 执行方式写的都是
「存量库手动 ALTER」—— 也就是说**从来没有可执行的迁移文件**。`create_all` 又只建缺失的**表**、
不给既有表加列, 于是任何建表早于该版本的库都会静默缺列, 直到某次真实下单才炸:

    pymysql.err.OperationalError: (1054, "Unknown column 'reduce_only' in 'field list'")

本机开发库正是这样, 由 `python -m at01_common.schema_check` 检出 6 处缺列。
本机制把这些列声明化, 让 `upgrade_schema` 每次启动幂等补齐 —— 不必再靠人记住
「当年那条 ALTER 我执行了吗」。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

import at01_common.models  # noqa: F401  确保模型注册进 metadata
from at01_common.database import Base, get_engine
from at01_common.migrations import _ADDITIVE_COLUMNS, _ensure_additive_columns, upgrade_schema


def test_declared_columns_exist_in_orm_metadata() -> None:
    """声明的每一列都必须真实存在于 ORM —— 防笔误/防模型改名后声明成为孤儿。"""
    for table, columns in _ADDITIVE_COLUMNS.items():
        assert table in Base.metadata.tables, f"声明的表不存在: {table}"
        actual = set(Base.metadata.tables[table].columns.keys())
        for column in columns:
            assert column in actual, f"{table}.{column} 在 ORM 里不存在"


def test_declared_columns_use_upper_case_sql_and_carry_a_default() -> None:
    """DDL 一律大写(可读性), 且**必须带 DEFAULT**。

    没有 DEFAULT 的 NOT NULL 列加到有数据的存量表上会直接失败 —— 存量库修复必须能在
    已有行上原地完成, 不能要求先清库。
    """
    for table, columns in _ADDITIVE_COLUMNS.items():
        for column, ddl in columns.items():
            assert "DEFAULT" in ddl.upper(), f"{table}.{column} 缺少 DEFAULT, 存量表加列会失败"
            assert ddl == ddl.upper(), f"{table}.{column} 的 DDL 应大写"


def test_v13_kill_origin_defaults_to_manual() -> None:
    """fail-closed: 存量库补列后既有行必须是 MANUAL(永远要人), 不能变成可自愈。"""
    ddl = _ADDITIVE_COLUMNS["kill_switch_state"]["origin"]
    assert "MANUAL" in ddl


@pytest.mark.asyncio
async def test_missing_column_is_added_and_repeat_is_noop(db_tables, monkeypatch) -> None:
    """机制本身: 缺列 → 补上; 再跑 → 不重复 ALTER(幂等)。"""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE probe_tbl (id INTEGER PRIMARY KEY, keep TEXT)"))

    monkeypatch.setitem(_ADDITIVE_COLUMNS, "probe_tbl", {"added": "VARCHAR(16) NOT NULL DEFAULT 'x'"})

    async with engine.begin() as conn:
        added = await _ensure_additive_columns(conn, "sqlite")
    assert added == ["probe_tbl.added"]

    # 幂等: 第二次不再新增
    async with engine.begin() as conn:
        assert await _ensure_additive_columns(conn, "sqlite") == []

    async with engine.connect() as conn:
        cols = {r[1] for r in (await conn.execute(text("PRAGMA table_info(probe_tbl)"))).fetchall()}
    assert "added" in cols


@pytest.mark.asyncio
async def test_existing_rows_get_the_default_value(db_tables, monkeypatch) -> None:
    """存量表已有数据时补列, 既有行必须拿到 DEFAULT —— 否则等于给历史数据编了个 NULL。"""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE probe_rows (id INTEGER PRIMARY KEY, keep TEXT)"))
        await conn.execute(text("INSERT INTO probe_rows (id, keep) VALUES (1, 'a')"))

    monkeypatch.setitem(
        _ADDITIVE_COLUMNS, "probe_rows", {"added": "VARCHAR(16) NOT NULL DEFAULT 'hist'"}
    )
    async with engine.begin() as conn:
        await _ensure_additive_columns(conn, "sqlite")

    async with engine.connect() as conn:
        value = (await conn.execute(text("SELECT added FROM probe_rows WHERE id=1"))).scalar()
    assert value == "hist"


@pytest.mark.asyncio
async def test_unknown_dialect_skips_instead_of_guessing(db_tables, monkeypatch) -> None:
    """方言不认识时**跳过**, 不猜着执行 ALTER —— 宁可下次再补, 也不写坏生产库。"""
    engine = get_engine()
    async with engine.begin() as conn:
        assert await _ensure_additive_columns(conn, "oracle") == []


@pytest.mark.asyncio
async def test_upgrade_schema_repairs_a_column_dropped_from_an_existing_table(
    db_tables, monkeypatch
) -> None:
    """端到端: 模拟「存量库缺列」, 跑 upgrade_schema 后应被修好。

    这正是本机开发库的真实遭遇(缺 orders.reduce_only 导致首次下单报 1054)。
    """
    engine = get_engine()
    # 真实表的真实缺列场景 —— 直接把 kill_switch_state.origin 删掉(MySQL 支持 DROP COLUMN;
    # SQLite 也支持)。这比造一张假表更接近生产。
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE kill_switch_state DROP COLUMN origin"))

    await upgrade_schema()

    async with engine.connect() as conn:
        cols = {
            r[1]
            for r in (await conn.execute(text("PRAGMA table_info(kill_switch_state)"))).fetchall()
        }
    assert "origin" in cols
