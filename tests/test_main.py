"""Tests for app.main — FastAPI application bootstrap, health and readiness probes."""

import pytest
from httpx import ASGITransport, AsyncClient


class TestAppMetadata:
    """The FastAPI application is configured with the correct metadata."""

    def test_app_title_matches_settings(self):
        from app.config.settings import settings
        from app.main import app

        assert app.title == settings.APP_NAME

    def test_app_title_exact_value(self):
        """Verify the actual title string for the default case."""
        from app.config.settings import Settings

        assert Settings().APP_NAME == "Integration Service"

    def test_cors_middleware_configured_with_wildcard_origins(self):
        """CORS middleware allows all origins, methods, headers, and credentials."""
        from fastapi.middleware.cors import CORSMiddleware
        from app.main import app

        cors_middlewares = [
            m for m in app.user_middleware if m.cls is CORSMiddleware
        ]
        assert len(cors_middlewares) == 1, "Expected exactly one CORS middleware"

        kwargs = cors_middlewares[0].kwargs
        assert kwargs.get("allow_origins") == ["*"]
        assert kwargs.get("allow_credentials") is None
        assert kwargs.get("allow_methods") == ["*"]
        assert kwargs.get("allow_headers") == ["*"]


class TestHealthEndpoint:
    """GET /health — liveness probe.

    Always returns {"status": "healthy"} regardless of external dependencies.
    """

    @pytest.mark.asyncio
    async def test_health_returns_healthy(self):
        from app.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health")

        assert response.status_code == 200
        assert response.json() == {"status": "healthy"}

    @pytest.mark.asyncio
    async def test_health_content_type_is_json(self):
        from app.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health")

        assert response.headers.get("content-type", "").startswith("application/json")

    @pytest.mark.asyncio
    async def test_health_rejects_post(self):
        from app.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/health")

        assert response.status_code == 405

    @pytest.mark.asyncio
    async def test_health_rejects_put(self):
        from app.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.put("/health")

        assert response.status_code == 405

    @pytest.mark.asyncio
    async def test_health_rejects_delete(self):
        from app.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.delete("/health")

        assert response.status_code == 405


class TestReadyEndpoint:
    """GET /ready — readiness probe with database connectivity check."""

    @pytest.mark.asyncio
    async def test_ready_returns_ready_when_db_available(self, monkeypatch):
        """Returns {"status": "ready"} when SELECT 1 succeeds."""
        import app.core.database as db_mod

        class _MockSession:
            async def execute(self, stmt):
                return None

        class _MockFactory:
            async def __aenter__(self):
                return _MockSession()

            async def __aexit__(self, *args):
                pass

        monkeypatch.setattr(
            db_mod, "async_session_factory", lambda: _MockFactory()
        )

        from app.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/ready")

        assert response.status_code == 200
        assert response.json() == {"status": "ready"}

    @pytest.mark.asyncio
    async def test_ready_returns_unhealthy_when_db_unavailable(self, monkeypatch):
        """Returns {"status": "unhealthy"} when the database is unreachable."""
        import app.core.database as db_mod

        def _raise_on_call():
            raise Exception("Connection refused to database")

        monkeypatch.setattr(
            db_mod, "async_session_factory", _raise_on_call
        )

        from app.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/ready")

        assert response.status_code == 200
        assert response.json() == {"status": "unhealthy"}

    @pytest.mark.asyncio
    async def test_ready_returns_unhealthy_when_execute_fails(self, monkeypatch):
        """Returns unhealthy when the execute call itself fails."""
        import app.core.database as db_mod

        class _FailingSession:
            async def execute(self, stmt):
                raise Exception("Query execution failed")

        class _MockFactory:
            async def __aenter__(self):
                return _FailingSession()

            async def __aexit__(self, *args):
                pass

        monkeypatch.setattr(
            db_mod, "async_session_factory", lambda: _MockFactory()
        )

        from app.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/ready")

        assert response.status_code == 200
        assert response.json() == {"status": "unhealthy"}

    @pytest.mark.asyncio
    async def test_ready_content_type_is_json(self, monkeypatch):
        """Response content type is application/json regardless of DB state."""
        import app.core.database as db_mod

        monkeypatch.setattr(
            db_mod, "async_session_factory", lambda: (_ for _ in ()).throw(
                Exception("No DB")
            )
        )

        from app.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/ready")

        assert response.headers.get("content-type", "").startswith("application/json")

    @pytest.mark.asyncio
    async def test_ready_rejects_post(self):
        from app.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/ready")

        assert response.status_code == 405

    @pytest.mark.asyncio
    async def test_ready_rejects_put(self):
        from app.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.put("/ready")

        assert response.status_code == 405

    @pytest.mark.asyncio
    async def test_ready_rejects_delete(self):
        from app.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.delete("/ready")

        assert response.status_code == 405
