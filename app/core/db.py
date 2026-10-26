from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

# 对照 Java：相当于 DataSource + SqlSessionFactory。
# 关键差异：SQLAlchemy 的 Session 不是线程安全的，必须"一个请求一个 Session"，
# 用下面的 get_db 依赖注入进去，请求结束自动关闭。
engine = create_async_engine(
    settings.database_url,
    echo=settings.db_echo,
    pool_pre_ping=True,  # 取连接前探活，避免长连接被服务端断掉后报错
    pool_size=10,
    max_overflow=20,
)

SessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,  # 提交后对象仍可读，避免再查一次库
    autoflush=False,
)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：请求级 Session。

    用法：
        async def endpoint(db: AsyncSession = Depends(get_db)): ...
    """
    async with SessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
