"""Tests for /health (liveness) and /ready (readiness) probes.

Uses the ``async_client`` fixture from conftest for ASGI transport and
``monkeypatch`` (pytest built-in) to simulate database states for the
readiness probe.
"""

import pytest


class TestHealthEndpoint:
    """GET /health — liveness probe.

    Always returns ``{"status": "healthy"}`` regardless of external
    dependencies.
    """

    @pytest.mark.asyncio
    async def test_health_returns_healthy(self, async_client):
        """GET /health returns 200 with {"status": "healthy"}."""
        response = await async_client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "healthy"}

    @pytest.mark.asyncio
    async def test_post_health_returns_405(self, async_client):
        """POST /health returns 405 Method Not Allowed."""
        response = await async_client.post("/health")
        assert response.status_code == 405


class TestReadyEndpoint:
    """GET /ready — readiness probe with database connectivity check.

    Returns ``{"status": "ready"}`` when ``SELECT 1`` succeeds and
    ``{"status": "unhealthy"}`` when the database is unreachable.
    """

    @pytest.mark.asyncio
    async def test_ready_returns_ready_when_db_available(
        self, async_client, monkeypatch
    ):
        """Returns {"status": "ready"} when SELECT 1 succeeds."""
        import app.core.database as db_mod

        class _MockSession:
            """Minimal stand-in that accepts any execute call."""

            async def execute(self, stmt):  # noqa: A003
                return None

        class _MockFactory:
            """Context-manager that yields a _MockSession."""

            async def __aenter__(self):
                return _MockSession()

            async def __aexit__(self, *args):
                pass

        monkeypatch.setattr(
            db_mod, "async_session_factory", lambda: _MockFactory()
        )

        response = await async_client.get("/ready")
        assert response.status_code == 200
        assert response.json() == {"status": "ready"}

    @pytest.mark.asyncio
    async def test_ready_returns_unhealthy_when_db_unavailable(
        self, async_client, monkeypatch
    ):
        """Returns {"status": "unhealthy"} when DB is unreachable."""
        import app.core.database as db_mod

        def _raise_on_call():
            raise Exception("Connection refused to database")

        monkeypatch.setattr(
            db_mod, "async_session_factory", _raise_on_call
        )

        response = await async_client.get("/ready")
        assert response.status_code == 200
        assert response.json() == {"status": "unhealthy"}

    @pytest.mark.asyncio
    async def test_post_ready_returns_405(self, async_client):
        """POST /ready returns 405 Method Not Allowed."""
        response = await async_client.post("/ready")
        assert response.status_code == 405
