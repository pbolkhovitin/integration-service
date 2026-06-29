"""Async-compatible Alembic environment configuration.

Uses ``run_async`` to wrap synchronous migration context execution so that
SQLAlchemy async engines (asyncpg) work correctly with Alembic.
"""

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

# Alembic Config object, which provides access to alembic.ini values.
config = context.config

# Set up Python logging from the ini file.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Import Base metadata so that --autogenerate can detect model changes.
# The models themselves are imported via app.models (for side-effect registration).
from app.core.database import Base  # noqa: E402
import app.models  # noqa: F401, E402 — ensure all models are registered on Base.metadata

target_metadata = Base.metadata


def get_url() -> str:
    """Return the database URL — environment variable wins, fallback to ini."""
    return os.getenv(
        "DATABASE_URL",
        config.get_main_option("sqlalchemy.url"),
    )


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL and not an Engine, though an
    Engine is acceptable here as well.  By skipping the Engine creation we
    don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given SQL string to the script
    output.
    """
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    """Configure a connection for migration context and run migrations."""
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Create an async engine and run migrations within an async context."""
    connectable = config.attributes.get("connection", None)

    if connectable is None:
        connectable = create_async_engine(get_url())

    if isinstance(connectable, AsyncEngine):
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
        await connectable.dispose()
    else:
        do_run_migrations(connectable)


def run_migrations_online() -> None:
    """Run migrations in 'online' mode — uses async engine via asyncio.run()."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
