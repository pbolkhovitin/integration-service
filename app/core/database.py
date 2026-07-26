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


def _build_database_url() -> str:
    """Build DATABASE_URL from environment variables.

    Prefers DATABASE_URL if set (e.g. in local dev).
    Otherwise, constructs from POSTGRES_* variables (Docker Compose).
    """
    url = os.getenv("DATABASE_URL")
    if url:
        return url

    user = os.getenv("POSTGRES_USER", "integration")
    password = os.getenv("POSTGRES_PASSWORD", "changeme")
    host = os.getenv("POSTGRES_SERVER", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    db = os.getenv("POSTGRES_DB", "integration")

    return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db}"


DATABASE_URL: str = _build_database_url()

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
