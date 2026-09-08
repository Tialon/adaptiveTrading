"""schema 快照 / 差异检查(V11.5 P0-4)

现状(审查结论): 项目无 Alembic; `init_db` 用 `Base.metadata.create_all` 只创建**缺失**
表、不对既有表做 ALTER(加列/改列/索引都不会传播到已存在的生产库)。`at90_deploy/init.sql`
仅 `CREATE DATABASE` 建库, 不手写表 DDL(表结构统一由 ORM 负责)。

现有测试 `test_v129_schema_audit.py` 钉死的是「ORM 元数据 vs 硬编码列清单」, 只能防
「改了 ORM 却忘了更新清单/版本」, 无法捕获「实际运行的数据库(生产库)缺列」—— 而这正是
`create_all` 静默忽略新列的真正风险点。

本模块补「实际库 vs ORM 元数据」的**名称级**差异检查(表名 + 列名), 供部署前/测试中
主动探测生产库 schema 漂移。类型级漂移(列类型/长度/默认值)暂不做: 名称级已覆盖 create_all
最危险坑(缺列), 类型级需 SQLite/MySQL 归一化, 复杂度和误报不划算(不强行引入 Alembic)。

用法:
    from at01_common.schema_check import check_schema
    drift = await check_schema()          # 用当前 settings 的 engine
    for d in drift: print(d)

CLI(部署前对现有库做漂移检查):
    python -m at01_common.schema_check    # 有漂移则 exit 1 并逐条打印
"""

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncEngine

# 内部/工具表(不参与漂移判定)
_INTERNAL_TABLE_PREFIX = "sqlite_"
_INTERNAL_TABLE_NAMES = {"alembic_version"}


def expected_schema() -> dict[str, set[str]]:
    """ORM 元数据作为 schema 真相: {表名: {列名, ...}}。"""
    import at01_common.models  # noqa: F401  确保所有模型注册进 metadata

    from at01_common.database import Base

    return {name: set(t.columns.keys()) for name, t in Base.metadata.tables.items()}


async def snapshot_schema(engine: AsyncEngine) -> dict[str, set[str]]:
    """对实际库做快照: {表名: {列名, ...}}(名称级, 过滤 sqlite_*/alembic_version)。"""
    async with engine.connect() as conn:

        def _snap(sync_conn) -> dict[str, set[str]]:
            insp = inspect(sync_conn)
            out: dict[str, set[str]] = {}
            for tname in insp.get_table_names():
                if tname.startswith(_INTERNAL_TABLE_PREFIX) or tname in _INTERNAL_TABLE_NAMES:
                    continue
                out[tname] = {c["name"] for c in insp.get_columns(tname)}
            return out

        return await conn.run_sync(_snap)


def diff_schema(expected: dict[str, set[str]], actual: dict[str, set[str]]) -> list[dict]:
    """纯函数差异: 返回漂移条目列表, 每条 {"type", "table", ["column"]}。

    type ∈ {missing_table, extra_table, missing_column, extra_column}。
    """
    drift: list[dict] = []
    for t in sorted(expected.keys() - actual.keys()):
        drift.append({"type": "missing_table", "table": t})
    for t in sorted(actual.keys() - expected.keys()):
        drift.append({"type": "extra_table", "table": t})
    for t in sorted(expected.keys() & actual.keys()):
        for c in sorted(expected[t] - actual[t]):
            drift.append({"type": "missing_column", "table": t, "column": c})
        for c in sorted(actual[t] - expected[t]):
            drift.append({"type": "extra_column", "table": t, "column": c})
    return drift


async def check_schema(engine: AsyncEngine | None = None) -> list[dict]:
    """快照实际库并与 ORM 元数据 diff; 无参数时用当前 engine。"""
    if engine is None:
        from at01_common.database import get_engine

        engine = get_engine()
    return diff_schema(expected_schema(), await snapshot_schema(engine))


def format_drift(drift: list[dict]) -> list[str]:
    """漂移条目 -> 可读行。"""
    lines: list[str] = []
    for d in drift:
        if d["type"] == "missing_table":
            lines.append(f"缺表 {d['table']}")
        elif d["type"] == "extra_table":
            lines.append(f"多表 {d['table']}")
        elif d["type"] == "missing_column":
            lines.append(f"缺列 {d['table']}.{d['column']}")
        elif d["type"] == "extra_column":
            lines.append(f"多列 {d['table']}.{d['column']}")
    return lines


async def _main() -> int:
    drift = await check_schema()
    if drift:
        for line in format_drift(drift):
            print(line)
        print(f"schema 漂移: {len(drift)} 处(详见 docs/database-migration.md)")
        return 1
    print("schema 无漂移(实际库 == ORM 元数据)")
    return 0


if __name__ == "__main__":
    import asyncio
    import sys

    sys.exit(asyncio.run(_main()))
