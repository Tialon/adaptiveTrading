"""V11.7 P1-1 证明: 迁移文件 checksum(SHA-256)落库 + 「已应用迁移被篡改」检测。

覆盖 `at01_common/migrations.py`:
- `checksum_of`: 内容 SHA-256(64 hex), 确定性、内容敏感、跨平台换行归一(不产生假漂移);
- `_ensure_checksum_column`: 旧 schema_version 表(V11.6 建, 无 checksum 列)补齐列, 幂等;
- `upgrade_schema`: 应用时把 checksum 随版本落库; 重复调用时校验已应用版本 checksum,
  被篡改 → RuntimeError(硬失败), 未篡改 → 幂等跳过。
"""

import pytest
from sqlalchemy import text

from at01_common.database import get_engine
from at01_common.migrations import (
    _ensure_checksum_column,
    checksum_of,
    upgrade_schema,
)


# ---------------------------------------------------------------------------
# checksum_of(纯函数)
# ---------------------------------------------------------------------------


def test_checksum_deterministic_and_sensitive(tmp_path):
    a = tmp_path / "a.sql"
    b = tmp_path / "b.sql"
    a.write_text("CREATE TABLE t1 (id INTEGER);", encoding="utf-8")
    b.write_text("CREATE TABLE t1 (id INTEGER);", encoding="utf-8")

    assert checksum_of(a) == checksum_of(b)  # 相同内容 → 相同 checksum
    assert len(checksum_of(a)) == 64
    assert all(c in "0123456789abcdef" for c in checksum_of(a))

    b.write_text("CREATE TABLE t1 (id INTEGER NOT NULL);", encoding="utf-8")  # 内容变化
    assert checksum_of(a) != checksum_of(b)


def test_checksum_normalizes_line_endings(tmp_path):
    """跨平台 checkout(git autocrlf)不应产生假漂移: \r\n 与 \n 归一为同一 checksum。"""
    crlf = tmp_path / "crlf.sql"
    lf = tmp_path / "lf.sql"
    crlf.write_bytes(b"CREATE TABLE t1 (id INTEGER);\r\n")
    lf.write_bytes(b"CREATE TABLE t1 (id INTEGER);\n")
    assert checksum_of(crlf) == checksum_of(lf)


# ---------------------------------------------------------------------------
# _ensure_checksum_column(旧表补齐)
# ---------------------------------------------------------------------------


async def test_ensure_checksum_column_adds_to_legacy_table():
    """V11.6 建的 schema_version 表无 checksum 列 → ALTER 补齐; 幂等不报错。"""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE schema_version ("
                "version TEXT PRIMARY KEY, applied_at TEXT, description TEXT)"
            )
        )
        await _ensure_checksum_column(conn, "sqlite")
        rows = (await conn.execute(text("PRAGMA table_info(schema_version)"))).fetchall()
        names = [r[1] for r in rows]
        assert "checksum" in names
        # 幂等: 再调用一次不报错(列已存在)
        await _ensure_checksum_column(conn, "sqlite")


# ---------------------------------------------------------------------------
# upgrade_schema: checksum 落库 + 篡改检测
# ---------------------------------------------------------------------------


async def test_upgrade_schema_stores_checksum(tmp_path):
    (tmp_path / "001_t1.sql").write_text(
        "CREATE TABLE IF NOT EXISTS t1 (id INTEGER PRIMARY KEY);", encoding="utf-8"
    )
    assert await upgrade_schema(tmp_path) == ["001"]

    engine = get_engine()
    async with engine.connect() as conn:
        row = (await conn.execute(text("SELECT checksum FROM schema_version"))).fetchone()
    assert row[0] == checksum_of(tmp_path / "001_t1.sql")
    assert len(row[0]) == 64


async def test_upgrade_schema_detects_tampering(tmp_path):
    f = tmp_path / "001_t1.sql"
    f.write_text("CREATE TABLE IF NOT EXISTS t1 (id INTEGER PRIMARY KEY);", encoding="utf-8")
    assert await upgrade_schema(tmp_path) == ["001"]

    # 事后篡改已应用迁移文件
    f.write_text("CREATE TABLE IF NOT EXISTS t1 (id INTEGER PRIMARY KEY, x TEXT);", encoding="utf-8")

    with pytest.raises(RuntimeError, match="内容已变更"):
        await upgrade_schema(tmp_path)


async def test_upgrade_schema_idempotent_preserves_checksum(tmp_path):
    f = tmp_path / "001_t1.sql"
    f.write_text("CREATE TABLE IF NOT EXISTS t1 (id INTEGER PRIMARY KEY);", encoding="utf-8")
    assert await upgrade_schema(tmp_path) == ["001"]
    assert await upgrade_schema(tmp_path) == []  # 未篡改 → 幂等跳过, 不报错
