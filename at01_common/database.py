"""
数据库配置(SQLAlchemy 2.x async)

engine 惰性创建(首次使用时绑定当前 settings),
支持测试环境重置(reset_engine)。
"""

import sqlite3

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import declarative_base

from at01_common.settings import get_settings

Base = declarative_base()


def _sqlite3_connection(dbapi_connection):
    """从 SQLAlchemy 连接对象取出底层 `sqlite3.Connection`(兼容 aiosqlite 适配层)。

    - 同步 `sqlite://` 方言: dbapi_connection 本身就是 `sqlite3.Connection`;
    - 异步 `sqlite+aiosqlite://` 方言: SQLAlchemy 用 `AsyncAdapt_aiosqlite_connection`
      包装 `aiosqlite.core.Connection`, 其 `.driver_connection._conn` 才是真正的
      `sqlite3.Connection`(aiosqlite 设 `check_same_thread=False`, 故可跨线程设 pragma)。
    - 其它方言(MySQL/aiomysql): 返回 None → 跳过。
    """
    if isinstance(dbapi_connection, sqlite3.Connection):
        return dbapi_connection
    driver = getattr(dbapi_connection, "driver_connection", None)
    if isinstance(driver, sqlite3.Connection):
        return driver
    if driver is not None:
        inner = getattr(driver, "_conn", None)
        if isinstance(inner, sqlite3.Connection):
            return inner
    return None


def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
    """SQLite 生产 pragma(WAL / busy_timeout / foreign_keys)。

    仅对 SQLite 连接生效(MySQL/aiomysql 连接取不到 `sqlite3.Connection`, 自动跳过)。
    - WAL: 读不阻塞写, 崩溃后 wal 文件可回放, 降低 Docker restart / Pi 掉电的损坏风险;
    - busy_timeout=5000ms: 并发写冲突时等待而非立即 `database is locked`;
    - foreign_keys=ON: 显式开启外键约束(SQLite 默认关闭), 保证关系一致性。
    """
    conn = _sqlite3_connection(dbapi_connection)
    if conn is None:
        return
    try:
        cursor = conn.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()
    except Exception:
        # 尽力而为: 某些 engine(如测试里未设 check_same_thread=False 的裸引擎)其底层
        # sqlite3.Connection 绑定创建线程, 跨线程设 pragma 会抛 ProgrammingError; 跳过即可,
        # 连接仍可用(仅缺 pragma 加固)。应用自身 engine(_ensure_engine)始终传
        # check_same_thread=False, 三项 pragma 会正常生效(由 test_v176 覆盖)。
        pass


# 注册到所有 Engine 连接(含 AsyncEngine 内部同步引擎), 连接建立即执行
event.listens_for(Engine, "connect")(_set_sqlite_pragmas)

# 数据库 schema 版本标记(非迁移框架, 仅作审计/告警锚点; 结构变更需同步递增并跑 schema 审计测试)
SCHEMA_VERSION = "V11.2"

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker | None = None


def _ensure_engine() -> AsyncEngine:
    """惰性创建引擎(每次检查 settings, 测试可换 URL)"""
    global _engine, _session_factory
    if _engine is None:
        settings = get_settings()
        # SQLite: check_same_thread=False 让 connect 事件跨线程设 pragma(aiosqlite 的底层
        # sqlite3.Connection 在其工作线程创建); timeout=5.0 即 busy_timeout=5000ms(每连接)。
        # 非 SQLite(MySQL/aiomysql)不传这些 connect_args。
        connect_args: dict = {}
        if (settings.database_url or "").startswith("sqlite"):
            connect_args = {"check_same_thread": False, "timeout": 5.0}
        _engine = create_async_engine(
            settings.database_url,
            pool_pre_ping=True,
            echo=settings.database_echo,
            connect_args=connect_args,
        )
        _session_factory = async_sessionmaker(
            _engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _engine


def reset_engine() -> None:
    """重置引擎(测试用: 切换 DATABASE_URL 后重建)"""
    global _engine, _session_factory
    if _engine is not None:
        try:
            import asyncio

            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 在运行中的循环里无法安全 dispose, 直接丢弃引用
                _engine = None
                _session_factory = None
                return
            loop.run_until_complete(_engine.dispose())
        except Exception:
            # 测试 reset 的 dispose 尽力而为: 失败仅丢弃引用, 不阻断后续重建(非关键路径)
            pass
    _engine = None
    _session_factory = None


def get_engine() -> AsyncEngine:
    """获取当前引擎"""
    return _ensure_engine()


class _SessionFactoryProxy:
    """session 工厂代理(保持 AsyncSessionLocal 可调用, 惰性绑定引擎)"""

    def __call__(self):
        _ensure_engine()
        return _session_factory()

    def __getattr__(self, name):
        _ensure_engine()
        return getattr(_session_factory, name)

    async def __aenter__(self):
        _ensure_engine()
        return await _session_factory().__aenter__()

    async def __aexit__(self, *args):
        return await _session_factory().__aexit__(*args)


AsyncSessionLocal = _SessionFactoryProxy()


async def get_db() -> AsyncSession:
    """FastAPI 依赖:获取数据库会话"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """建表 + 应用前向迁移(幂等)"""
    # 确保所有模型已注册
    import at01_common.models  # noqa: F401

    engine = _ensure_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # V11.6 P1-4: create_all 只建缺失表、不对既有表 ALTER; 此处补最小迁移框架,
    # 只应用未落库的 migrations/*.sql(幂等)。空库首启: 记 001_baseline 为已应用。
    from at01_common.migrations import upgrade_schema

    await upgrade_schema()

    from at01_common.logger import get_logger

    get_logger("DB").info(
        "数据库表已就绪(create_all + migrations)",
        schema_version=SCHEMA_VERSION,
        tables=len(Base.metadata.tables),
    )


async def close_db() -> None:
    """关闭连接池"""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
