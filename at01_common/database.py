"""
数据库配置(SQLAlchemy 2.x async)

engine 惰性创建(首次使用时绑定当前 settings),
支持测试环境重置(reset_engine)。
"""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import declarative_base

from at01_common.settings import get_settings

Base = declarative_base()

# 数据库 schema 版本标记(非迁移框架, 仅作审计/告警锚点; 结构变更需同步递增并跑 schema 审计测试)
SCHEMA_VERSION = "V11.2"

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker | None = None


def _ensure_engine() -> AsyncEngine:
    """惰性创建引擎(每次检查 settings, 测试可换 URL)"""
    global _engine, _session_factory
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            pool_pre_ping=True,
            echo=settings.database_echo,
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
    """建表"""
    # 确保所有模型已注册
    import at01_common.models  # noqa: F401

    engine = _ensure_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    from at01_common.logger import get_logger

    get_logger("DB").info(
        "数据库表已就绪(create_all)", schema_version=SCHEMA_VERSION, tables=len(Base.metadata.tables),
    )


async def close_db() -> None:
    """关闭连接池"""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
