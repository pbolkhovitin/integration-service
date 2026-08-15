"""Tests for admin-token auth on mutating /api/bitrix24/sync/* endpoints.

Mutating endpoints (trigger, cleanup, retry, reverse-test) require the
``X-Admin-Token`` header matching ``ADMIN_API_TOKEN``. Read-only status
endpoints remain open.
"""

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.config.settings import settings
from app.main import app


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test"
    ) as c:
        yield c


async def test_trigger_without_token_401(client) -> None:
    resp = await client.post("/api/bitrix24/sync/trigger")
    assert resp.status_code == 401


async def test_trigger_with_wrong_token_401(client) -> None:
    resp = await client.post(
        "/api/bitrix24/sync/trigger",
        headers={"X-Admin-Token": "wrong-token"},
    )
    assert resp.status_code == 401


async def test_trigger_with_valid_token_200(monkeypatch, client) -> None:
    monkeypatch.setattr(settings, "ADMIN_API_TOKEN", SecretStr("secret-123"))
    monkeypatch.setattr("app.api.bitrix._poll_bitrix24", AsyncMock())

    resp = await client.post(
        "/api/bitrix24/sync/trigger",
        headers={"X-Admin-Token": "secret-123"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "completed"}


async def test_cleanup_without_token_401(client) -> None:
    resp = await client.post("/api/bitrix24/sync/cleanup")
    assert resp.status_code == 401


async def test_reverse_test_without_token_401(client) -> None:
    resp = await client.post("/api/bitrix24/sync/reverse-test")
    assert resp.status_code == 401


async def test_retry_without_token_401(client) -> None:
    resp = await client.post("/api/bitrix24/sync/retry")
    assert resp.status_code == 401


async def test_status_endpoints_stay_open(client) -> None:
    resp = await client.get("/api/bitrix24/sync/status")
    assert resp.status_code == 200

    resp = await client.get("/api/bitrix24/sync/reverse-status")
    assert resp.status_code == 200