"""
Async database engine, session factory, and Base for SQLAlchemy 2.0.

Usage:
    from app.core.database import async_session_factory, Base, get_db

    # FastAPI dependency
    @app.get("/items")
    async def get_items(db: AsyncSession = Depends(get_db)):
        ...
"""

import os
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base

DATABASE_URL: str = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://integration:changeme@localhost:5432/integration",
)

engine = create_async_engine(DATABASE_URL, echo=False, future=True)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

Base = declarative_base()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields an async database session.

    The session is automatically closed when the request finishes.
    """
    async with async_session_factory() as session:
        try:
            yield session
        finally:
            await session.close()
