"""Tests for POST /webhook/bitrix/lead endpoint.

Uses conftest fixtures:
    ``async_client`` — ``httpx.AsyncClient`` with ASGITransport.
    ``mock_db_session`` — ``AsyncMock`` session with Task-aware add/commit.
    ``mock_glpi_client`` — ``MagicMock`` patched into GLPIClient.

Follows the same mocking patterns as ``test_bitrix.py``.
"""

import uuid
from unittest.mock import MagicMock

import pytest

from app.models.task import Task, TaskStatus


# ===================================================================
# Test helpers
# ===================================================================


def _make_task(**overrides: object) -> Task:
    """Create a ``Task`` with required fields and a valid UUID ``id``."""
    task = Task(
        source=overrides.get("source", "bitrix24"),  # type: ignore[arg-type]
        source_id=overrides.get("source_id", "src-1"),  # type: ignore[arg-type]
        type=overrides.get("type", "create_ticket"),  # type: ignore[arg-type]
        status=overrides.get("status", TaskStatus.PENDING),  # type: ignore[arg-type]
        idempotency_key=overrides.get("idempotency_key"),  # type: ignore[arg-type]
    )
    task.id = uuid.uuid4()
    return task


def _configure_existing_task(mock_db, task: Task | None) -> None:
    """Configure *mock_db.execute* to return *task* via
    ``scalar_one_or_none``."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = task
    mock_db.execute.return_value = result


# ===================================================================
# Happy path
# ===================================================================


class TestWebhookHappyPath:
    """New leads that reach GLPI and return success."""

    @pytest.mark.asyncio
    async def test_without_idempotency_key(
        self, async_client, mock_db_session, mock_glpi_client
    ):
        """Lead without idempotency_key creates a GLPI ticket and
        returns 200 with status ``success``."""
        resp = await async_client.post(
            "/webhook/bitrix/lead",
            json={"name": "John Doe", "phone": "+1-555-0001"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert uuid.UUID(data["task_id"])

    @pytest.mark.asyncio
    async def test_with_idempotency_key(
        self, async_client, mock_db_session, mock_glpi_client
    ):
        """Lead with a fresh idempotency_key creates a GLPI ticket
        and returns 200 with status ``success``."""
        # Ensure no existing task is found for this idempotency key
        _configure_existing_task(mock_db_session, None)
        resp = await async_client.post(
            "/webhook/bitrix/lead",
            json={"name": "Jane Doe", "idempotency_key": "fresh-key-002"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert uuid.UUID(data["task_id"])


# ===================================================================
# Idempotency
# ===================================================================


class TestWebhookIdempotency:
    """Existing idempotency keys return the correct terminal status
    without calling GLPI."""

    @pytest.mark.asyncio
    async def test_completed_task_returns_duplicate(
        self, async_client, mock_db_session
    ):
        """COMPLETED task with the same idempotency_key
        returns ``duplicate``."""
        existing = _make_task(
            status=TaskStatus.COMPLETED,
            idempotency_key="dup-completed",
        )
        _configure_existing_task(mock_db_session, existing)

        resp = await async_client.post(
            "/webhook/bitrix/lead",
            json={"name": "Lead", "idempotency_key": "dup-completed"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "duplicate"
        assert data["task_id"] == str(existing.id)

    @pytest.mark.asyncio
    async def test_failed_task_returns_duplicate(
        self, async_client, mock_db_session
    ):
        """FAILED task with the same idempotency_key
        returns ``duplicate``."""
        existing = _make_task(
            status=TaskStatus.FAILED,
            idempotency_key="dup-failed",
        )
        _configure_existing_task(mock_db_session, existing)

        resp = await async_client.post(
            "/webhook/bitrix/lead",
            json={"name": "Lead", "idempotency_key": "dup-failed"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "duplicate"
        assert data["task_id"] == str(existing.id)

    @pytest.mark.asyncio
    async def test_processing_task_returns_in_progress(
        self, async_client, mock_db_session
    ):
        """PROCESSING task with the same idempotency_key
        returns ``in_progress``."""
        existing = _make_task(
            status=TaskStatus.PROCESSING,
            idempotency_key="ip-processing",
        )
        _configure_existing_task(mock_db_session, existing)

        resp = await async_client.post(
            "/webhook/bitrix/lead",
            json={"name": "Lead", "idempotency_key": "ip-processing"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "in_progress"
        assert data["task_id"] == str(existing.id)

    @pytest.mark.asyncio
    async def test_pending_task_returns_in_progress(
        self, async_client, mock_db_session
    ):
        """PENDING task with the same idempotency_key
        returns ``in_progress``."""
        existing = _make_task(
            status=TaskStatus.PENDING,
            idempotency_key="ip-pending",
        )
        _configure_existing_task(mock_db_session, existing)

        resp = await async_client.post(
            "/webhook/bitrix/lead",
            json={"name": "Lead", "idempotency_key": "ip-pending"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "in_progress"
        assert data["task_id"] == str(existing.id)


# ===================================================================
# Validation errors
# ===================================================================


class TestWebhookValidation:
    """Badly-formed requests return 422."""

    @pytest.mark.asyncio
    async def test_missing_name_returns_422(
        self, async_client, mock_db_session, mock_glpi_client
    ):
        """POST without the required ``name`` field returns 422."""
        resp = await async_client.post(
            "/webhook/bitrix/lead",
            json={"phone": "+7"},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_empty_body_returns_422(
        self, async_client, mock_db_session, mock_glpi_client
    ):
        """POST with an empty JSON body returns 422."""
        resp = await async_client.post(
            "/webhook/bitrix/lead",
            json={},
        )
        assert resp.status_code == 422


# ===================================================================
# GLPI failures
# ===================================================================


class TestWebhookGlpiFailure:
    """When GLPI is unreachable the endpoint returns 500."""

    @pytest.mark.asyncio
    async def test_glpi_init_session_failure_returns_500(
        self, async_client, mock_db_session, mock_glpi_client
    ):
        """GLPI init_session failure returns 500 with a generic
        error message (no internal details leaked)."""
        mock_glpi_client.init_session.side_effect = RuntimeError(
            "GLPI unreachable"
        )

        resp = await async_client.post(
            "/webhook/bitrix/lead",
            json={"name": "Fail Lead"},
        )

        assert resp.status_code == 500
        # Handler must NOT leak internal error details
        detail = resp.json().get("detail", "")
        assert "GLPI unreachable" not in detail
        assert detail == "GLPI processing failed"
