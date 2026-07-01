"""
FastAPI application bootstrap for the Integration Service.

Provides the FastAPI app instance, lifespan management (startup/shutdown),
CORS middleware, and basic /health + /ready probes.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api.bitrix import router as bitrix_router
from app.config.settings import settings
from app.core import database

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage the application lifecycle.

    Startup: logs that the application is starting.  The database engine is
    lazy (connections are created on first query), so no explicit connection
    is established here.

    Shutdown: disposes the async engine connection pool, if the engine was
    ever used.
    """
    logger.info("Starting %s", settings.APP_NAME)
    yield
    await database.engine.dispose()
    logger.info("Shutdown complete")


app = FastAPI(
    title=settings.APP_NAME,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(bitrix_router)


@app.get("/health")
async def health():
    """Liveness probe.

    Returns immediately with a healthy status regardless of external
    dependencies.  Useful for container orchestrators that check if the
    process is alive.
    """
    return {"status": "healthy"}


@app.get("/ready")
async def ready():
    """Readiness probe.

    Performs a lightweight database connectivity check (``SELECT 1``).
    Returns ``{"status": "ready"}`` on success or ``{"status": "unhealthy"}``
    if the database is unreachable.
    """
    try:
        async with database.async_session_factory() as session:
            await asyncio.wait_for(
                session.execute(text("SELECT 1")),
                timeout=5,
            )
        return {"status": "ready"}
    except Exception:
        logger.exception("Readiness check failed")
        return {"status": "unhealthy"}
