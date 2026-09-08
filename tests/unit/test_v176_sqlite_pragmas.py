"""V11.8 §12 证明: SQLite 生产 pragma(WAL / busy_timeout / foreign_keys)。

覆盖 `at01_common/database.py::_set_sqlite_pragmas` + `event.listens_for(Engine, "connect")`:
- 非 sqlite 连接(MySQL/aiomysql)直接跳过, 不碰 cursor;
- sqlite 连接: busy_timeout=5000 / foreign_keys=ON 生效;
- 真实 async engine 建立连接即应用 pragma(journal_mode=WAL / busy_timeout / foreign_keys)。

动机(Docker restart / Pi 掉电下 SQLite 稳定性): WAL 读不阻塞写 + 崩溃可回放,
busy_timeout 避免 `database is locked`, foreign_keys 显式开启约束(默认关)。
"""

import sqlite3

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from at01_common.database import _set_sqlite_pragmas


# ---------------------------------------------------------------------------
# 纯函数: 非 sqlite 跳过
# ---------------------------------------------------------------------------


def test_skips_non_sqlite_connection():
    class FakeConn:
        def cursor(self):
            raise AssertionError("非 sqlite 连接不应访问 cursor")

    # 不应抛异常(直接 return)
    _set_sqlite_pragmas(FakeConn(), None)


# ---------------------------------------------------------------------------
# 纯函数: sqlite 连接应用 busy_timeout / foreign_keys
# ---------------------------------------------------------------------------


def test_applies_pragmas_on_sqlite_connection():
    conn = sqlite3.connect(":memory:")
    try:
        _set_sqlite_pragmas(conn, None)
        cur = conn.cursor()
        cur.execute("PRAGMA busy_timeout")
        assert cur.fetchone()[0] == 5000
        cur.execute("PRAGMA foreign_keys")
        assert cur.fetchone()[0] == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 集成: engine 建立连接即应用 pragma
# ---------------------------------------------------------------------------


async def test_engine_connect_applies_pragmas(tmp_path):
    db = tmp_path / "pragmas.db"
    # 与 _ensure_engine 一致: check_same_thread=False 让 connect 事件跨线程设 pragma
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db}",
        connect_args={"check_same_thread": False},
    )
    try:
        async with engine.connect() as conn:
            mode = (await conn.execute(text("PRAGMA journal_mode"))).scalar()
            busy = (await conn.execute(text("PRAGMA busy_timeout"))).scalar()
            fk = (await conn.execute(text("PRAGMA foreign_keys"))).scalar()
    finally:
        await engine.dispose()

    assert str(mode).lower() == "wal"
    assert busy == 5000
    assert fk == 1
