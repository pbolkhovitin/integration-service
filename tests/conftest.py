"""Shared test fixtures for the Integration Service.

Compatible with existing patterns in test_main.py, test_settings.py,
test_glpi.py, and test_bitrix.py.

Fixtures (all ``autouse=False`` so tests opt-in):
    ``async_client``
        ``httpx.AsyncClient`` over ``ASGITransport`` for the FastAPI app.
    ``mock_db_session``
        ``AsyncMock`` scoped session injected via ``get_db`` dependency
        override.  ``add`` and ``commit`` are pre-configured to track
        ``Task`` instances and assign UUIDs on commit.
    ``mock_glpi_client``
        ``MagicMock`` patched into ``app.api.bitrix.GLPIClient`` with
        sensible defaults for ``init_session`` and ``create_ticket``.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport

from app.main import app
from app.models.task import Task


@pytest.fixture
async def async_client():
    """Create an ``httpx.AsyncClient`` with ``ASGITransport`` for the
    FastAPI application.

    Usage::

        @pytest.mark.asyncio
        async def test_health(self, async_client):
            resp = await async_client.get("/health")
    """
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        yield client


@pytest.fixture
def mock_db_session():
    """Provide a mock async DB session injected via ``get_db`` dependency
    override.

    The yielded ``AsyncMock`` has ``add`` and ``commit`` pre-configured:

    * ``add`` — captures any ``Task`` instance into an internal list.
    * ``commit`` — assigns ``uuid.UUID`` to any ``Task`` with ``id is None``,
      simulating what a real database flush/commit does.

    Configure ``execute`` per-test to control idempotency lookups::

        result = MagicMock()
        result.scalar_one_or_none.return_value = existing_task
        mock_db_session.execute.return_value = result

    The fixture registers itself on setup and restores the original
    dependency on teardown.
    """
    from app.core.database import get_db

    session = AsyncMock()
    session._added_tasks: list[Task] = []

    def _add_side_effect(obj: object) -> None:
        if isinstance(obj, Task):
            session._added_tasks.append(obj)

    async def _commit_side_effect() -> None:
        for t in session._added_tasks:
            if t.id is None:
                t.id = uuid.uuid4()

    # session.add is a synchronous call (SQLAlchemy AsyncSession.add is sync)
    # so we MUST use MagicMock, not AsyncMock — AsyncMock side effects only
    # fire when awaited, and the real code never awaits db.add().
    session.add = MagicMock(side_effect=_add_side_effect)
    session.commit.side_effect = _commit_side_effect

    async def _override():
        yield session

    app.dependency_overrides[get_db] = _override
    try:
        yield session
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def mock_glpi_client():
    """Patch ``app.api.bitrix.GLPIClient`` with a ``MagicMock``.

    The yielded mock instance has ``init_session`` and ``create_ticket``
    pre-configured with sensible defaults.  Override in individual tests::

        mock_glpi_client.init_session.return_value = "custom-token"
        mock_glpi_client.init_session.side_effect = RuntimeError(...)
        mock_glpi_client.create_ticket.return_value = {"id": 99}
    """
    instance = MagicMock()
    instance.init_session.return_value = "sess-test"
    instance.create_ticket.return_value = {"id": 1, "message": "created"}

    with patch("app.api.bitrix.GLPIClient", return_value=instance):
        yield instance


@pytest.fixture(autouse=True)
def patch_to_thread():
    """Run ``asyncio.to_thread`` synchronously (no real OS threads)."""
    with patch("app.api.bitrix.asyncio.to_thread", side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs)):
        yield
