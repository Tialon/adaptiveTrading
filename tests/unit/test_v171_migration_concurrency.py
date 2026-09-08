"""V11.7 P1-2 证明: 迁移并发安全(进程内 asyncio 互斥)。

覆盖 `at01_common/migrations.py::upgrade_schema` 的 `_migration_lock`:
- 同一事件循环里多个 `upgrade_schema` 并发(gather)→ 串行化, 只应用一次;
- 并发后仍幂等(重复调用 no-op)。

无锁时, 并发调用会双读「未应用」并双 INSERT → 唯一键冲突/双应用; 本测试锁定「只应用一次」。
"""

import asyncio

from at01_common.migrations import upgrade_schema


async def test_concurrent_upgrade_schema_applies_once(tmp_path):
    f = tmp_path / "001_t1.sql"
    f.write_text(
        "CREATE TABLE IF NOT EXISTS t1 (id INTEGER PRIMARY KEY);", encoding="utf-8"
    )

    results = await asyncio.gather(
        upgrade_schema(tmp_path),
        upgrade_schema(tmp_path),
        upgrade_schema(tmp_path),
    )
    applied = [v for r in results for v in r]
    assert applied == ["001"]  # 三个并发调用只应用一次, 其余重读后跳过

    # 并发应用后仍幂等
    assert await upgrade_schema(tmp_path) == []


async def test_concurrent_multi_migration_applies_once_each(tmp_path):
    (tmp_path / "001_a.sql").write_text(
        "CREATE TABLE IF NOT EXISTS a (id INTEGER PRIMARY KEY);", encoding="utf-8"
    )
    (tmp_path / "002_b.sql").write_text(
        "CREATE TABLE IF NOT EXISTS b (id INTEGER PRIMARY KEY);", encoding="utf-8"
    )

    results = await asyncio.gather(
        upgrade_schema(tmp_path), upgrade_schema(tmp_path)
    )
    applied = [v for r in results for v in r]
    assert applied == ["001", "002"]

    # 幂等
    assert await upgrade_schema(tmp_path) == []
