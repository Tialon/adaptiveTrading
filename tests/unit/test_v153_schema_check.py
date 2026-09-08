"""V11.5 P0-4 schema 差异检查(实际库 vs ORM 元数据)。

补 `test_v129_schema_audit.py` 的盲区: 后者钉死「ORM 元数据 vs 硬编码列清单」, 只能防
「改了 ORM 却忘了同步清单/版本」; 本测试验证「实际运行库 vs ORM 元数据」的差异检查,
能捕获 `create_all` 静默忽略的「生产库缺列」(create_all 只建缺失表、不对既有表 ALTER)。

覆盖:
1. `create_all` 之后实际库 schema == ORM 元数据(无漂移);
2. 快照反映**实际库**(而非 ORM 元数据), 并能检测缺列(手工造一张缺列的 orders 表);
3. `diff_schema` 纯函数覆盖四类漂移(缺表/多表/缺列/多列)。
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

import at01_common.models  # noqa: F401  确保所有模型注册进 metadata
from at01_common import schema_check
from at01_common.database import Base


async def test_expected_schema_matches_metadata():
    """expected_schema 直接来自 ORM 元数据。"""
    exp = schema_check.expected_schema()
    meta = {name: set(t.columns.keys()) for name, t in Base.metadata.tables.items()}
    assert exp == meta
    assert "orders" in exp
    assert "filled_quantity" in exp["orders"]


async def test_fresh_create_all_no_drift(db_tables):
    """create_all 之后, 实际库与 ORM 元数据一致(无漂移)。"""
    drift = await schema_check.check_schema()
    assert drift == [], schema_check.format_drift(drift)


def test_diff_detects_all_drift_types():
    """纯 diff 覆盖缺表 / 多表 / 缺列 / 多列 四类。"""
    expected = {
        "orders": {"id", "symbol", "status"},
        "positions": {"id", "symbol"},
        "signals": {"id", "symbol"},
    }
    actual = {
        "orders": {"id", "symbol"},  # 缺 status
        "positions": {"id", "symbol", "extra_col"},  # 多 extra_col
        "foo": {"id"},  # 多表
    }
    drift = schema_check.diff_schema(expected, actual)

    types = {(d["type"], d["table"], d.get("column")) for d in drift}
    assert ("missing_table", "signals", None) in types
    assert ("extra_table", "foo", None) in types
    assert ("missing_column", "orders", "status") in types
    assert ("extra_column", "positions", "extra_col") in types


def test_format_drift_readable():
    drift = [
        {"type": "missing_table", "table": "orders"},
        {"type": "missing_column", "table": "orders", "column": "status"},
        {"type": "extra_column", "table": "positions", "column": "x"},
        {"type": "extra_table", "table": "foo"},
    ]
    lines = schema_check.format_drift(drift)
    assert "缺表 orders" in lines
    assert "缺列 orders.status" in lines
    assert "多列 positions.x" in lines
    assert "多表 foo" in lines


async def test_snapshot_reflects_real_db_and_detects_missing_column():
    """快照读的是**实际库**列, 而非 ORM 元数据; 缺列能被检测。

    手工造一张只有 3 列的 orders 表(模拟 create_all 静默忽略新列后的生产库),
    快照应只含这 3 列, diff 应报 ORM 有、实际库缺的列(如 filled_quantity)。
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE TABLE orders ("
                    "id INTEGER PRIMARY KEY, symbol VARCHAR(20), status VARCHAR(16))"
                )
            )

        actual = await schema_check.snapshot_schema(engine)
        # 快照只反映真实库(不是 ORM 元数据: 只有 orders 一张表、3 列)
        assert set(actual.keys()) == {"orders"}
        assert actual["orders"] == {"id", "symbol", "status"}

        drift = schema_check.diff_schema(schema_check.expected_schema(), actual)
        missing = {
            d["column"] for d in drift if d["type"] == "missing_column" and d["table"] == "orders"
        }
        assert "filled_quantity" in missing
    finally:
        await engine.dispose()
