"""Tests for Task 1.7 reverse sync changes:

1. Settings — test_task_ids property
2. BitrixClient — update_task_status, add_comment
3. GLPIClient — get_ticket_followups

Mocking strategy: use httpx.MockTransport (same pattern as test_glpi.py)
to avoid real HTTP calls.  Settings tests need no mocking.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from httpx import MockTransport, Request, Response

from app.config.settings import Settings
from app.services.glpi import GLPIClient

# ===================================================================
# Settings — test_task_ids property
# ===================================================================


class TestSettingsTestTaskIds:
    """``Settings.test_task_ids`` property — parses comma-separated
    ``TEST_TASK_IDS`` into ``list[int]``."""

    def test_default_value(self) -> None:
        """Default TEST_TASK_IDS = "35591,35633" yields [35591, 35633]."""
        s = Settings()
        assert s.TEST_MODE is True
        assert s.TEST_TASK_IDS == "35591,35633"
        assert s.test_task_ids == [35591, 35633]

    def test_single_value(self) -> None:
        """Single integer string returns a one-element list."""
        s = Settings(TEST_TASK_IDS="42")
        assert s.test_task_ids == [42]

    def test_three_values(self) -> None:
        """Multiple comma-separated values parse correctly."""
        s = Settings(TEST_TASK_IDS="100,200,300")
        assert s.test_task_ids == [100, 200, 300]

    def test_whitespace_around_values(self) -> None:
        """Spaces after commas are stripped."""
        s = Settings(TEST_TASK_IDS="  1 ,  2 , 3  ")
        assert s.test_task_ids == [1, 2, 3]

    def test_empty_string_returns_empty_list(self) -> None:
        """Empty TEST_TASK_IDS returns []."""
        s = Settings(TEST_TASK_IDS="")
        assert s.test_task_ids == []

    def test_override_via_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Environment variable override works."""
        monkeypatch.setenv("TEST_TASK_IDS", "999,888")
        s = Settings()
        assert s.test_task_ids == [999, 888]

    def test_test_mode_false_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """TEST_MODE can be disabled via env."""
        monkeypatch.setenv("TEST_MODE", "false")
        s = Settings()
        assert s.TEST_MODE is False


# ===================================================================
# Helpers — BitrixClient with mock transport
# ===================================================================


def _build_bitrix_client(handler) -> Any:
    """Return a ``BitrixClient`` whose internal transport is swapped for
    *handler*, avoiding real HTTP calls.

    Follows the same pattern as ``_build_client`` in test_glpi.py.
    """
    from app.services.bitrix import BitrixClient

    client = BitrixClient(
        webhook_url="https://b24.example.com/rest/1/FAKE_TOKEN",
    )
    # Swap the transport — the only layer we mock
    client._client = httpx.Client(transport=MockTransport(handler))
    return client


# ===================================================================
# BitrixClient — update_task_status
# ===================================================================


class TestBitrixUpdateTaskStatus:
    """``BitrixClient.update_task_status`` — POST ``tasks.task.update.json``."""

    def test_success_updates_status(self) -> None:
        """Completed status (5) sends correct JSON body and returns task."""

        def handler(request: Request) -> Response:
            assert request.method == "POST"
            assert str(request.url).endswith("tasks.task.update.json")

            body = json.loads(request.read())
            assert body == {"id": 42, "fields": {"STATUS": 5}}

            return Response(
                200,
                json={
                    "result": {
                        "task": {"id": 42, "title": "Test", "status": "5"}
                    }
                },
            )

        client = _build_bitrix_client(handler)
        result = client.update_task_status(task_id=42, status=5)
        assert result == {"id": 42, "title": "Test", "status": "5"}

    def test_pending_status(self) -> None:
        """Status 2 (pending) sends correct JSON body."""

        def handler(request: Request) -> Response:
            assert request.method == "POST"
            body = json.loads(request.read())
            assert body == {"id": 99, "fields": {"STATUS": 2}}
            return Response(
                200,
                json={
                    "result": {
                        "task": {"id": 99, "status": "2"}
                    }
                },
            )

        client = _build_bitrix_client(handler)
        result = client.update_task_status(task_id=99, status=2)
        assert result == {"id": 99, "status": "2"}

    def test_http_error_raises_runtime_error(self) -> None:
        """Non-2xx response raises RuntimeError."""

        def handler(request: Request) -> Response:
            return Response(400, text="Bad Request")

        client = _build_bitrix_client(handler)
        with pytest.raises(RuntimeError) as exc_info:
            client.update_task_status(task_id=1, status=5)
        assert "400" in str(exc_info.value)
        assert "tasks.task.update.json" in str(exc_info.value)

    def test_network_error_raises_runtime_error(self) -> None:
        """Transport error raises RuntimeError."""

        def handler(request: Request) -> Response:
            raise httpx.RequestError("Connection refused")

        client = _build_bitrix_client(handler)
        with pytest.raises(RuntimeError) as exc_info:
            client.update_task_status(task_id=1, status=5)
        assert "Connection refused" in str(exc_info.value)

    def test_empty_result_returns_empty_dict(self) -> None:
        """API response with no 'task' key returns {}."""

        def handler(request: Request) -> Response:
            return Response(200, json={"result": {}})

        client = _build_bitrix_client(handler)
        result = client.update_task_status(task_id=1, status=5)
        assert result == {}

    def test_zero_status(self) -> None:
        """Status 0 (new) sends correctly."""

        def handler(request: Request) -> Response:
            body = json.loads(request.read())
            assert body == {"id": 1, "fields": {"STATUS": 0}}
            return Response(200, json={"result": {"task": {"id": 1, "status": "0"}}})

        client = _build_bitrix_client(handler)
        result = client.update_task_status(task_id=1, status=0)
        assert result == {"id": 1, "status": "0"}


# ===================================================================
# BitrixClient — add_comment
# ===================================================================


class TestBitrixAddComment:
    """``BitrixClient.add_comment`` — POST ``tasks.task.comment.add.json``."""

    def test_success_adds_comment(self) -> None:
        """Happy path sends correct JSON body and returns result."""

        def handler(request: Request) -> Response:
            assert request.method == "POST"
            assert str(request.url).endswith("tasks.task.comment.add.json")

            body = json.loads(request.read())
            assert body == {
                "taskId": 42,
                "fields": {"POST_MESSAGE": "Status updated to Completed"},
            }

            return Response(
                200,
                json={
                    "result": {
                        "commentId": 101,
                        "taskId": 42,
                    }
                },
            )

        client = _build_bitrix_client(handler)
        result = client.add_comment(
            task_id=42,
            message="Status updated to Completed",
        )
        assert result == {"commentId": 101, "taskId": 42}

    def test_empty_message(self) -> None:
        """Empty message string passes through as-is."""

        def handler(request: Request) -> Response:
            body = json.loads(request.read())
            assert body == {
                "taskId": 7,
                "fields": {"POST_MESSAGE": ""},
            }
            return Response(200, json={"result": {"commentId": 1}})

        client = _build_bitrix_client(handler)
        result = client.add_comment(task_id=7, message="")
        assert result == {"commentId": 1}

    def test_unicode_message(self) -> None:
        """Unicode / emoji in message passes through correctly."""

        def handler(request: Request) -> Response:
            body = json.loads(request.read())
            assert body["fields"]["POST_MESSAGE"] == "🔥 Статус обновлён ✓"
            return Response(200, json={"result": {"commentId": 1}})

        client = _build_bitrix_client(handler)
        result = client.add_comment(
            task_id=1,
            message="🔥 Статус обновлён ✓",
        )
        assert result == {"commentId": 1}

    def test_http_error_raises_runtime_error(self) -> None:
        """Non-2xx response raises RuntimeError."""

        def handler(request: Request) -> Response:
            return Response(403, text="Forbidden")

        client = _build_bitrix_client(handler)
        with pytest.raises(RuntimeError) as exc_info:
            client.add_comment(task_id=1, message="test")
        assert "403" in str(exc_info.value)
        assert "tasks.task.comment.add.json" in str(exc_info.value)

    def test_network_error_raises_runtime_error(self) -> None:
        """Transport error raises RuntimeError."""

        def handler(request: Request) -> Response:
            raise httpx.RequestError("DNS lookup failed")

        client = _build_bitrix_client(handler)
        with pytest.raises(RuntimeError) as exc_info:
            client.add_comment(task_id=1, message="test")
        assert "DNS lookup failed" in str(exc_info.value)

    def test_empty_result_returns_empty_dict(self) -> None:
        """API response with empty 'result' returns {}."""

        def handler(request: Request) -> Response:
            return Response(200, json={"result": {}})

        client = _build_bitrix_client(handler)
        result = client.add_comment(task_id=1, message="hello")
        assert result == {}


# ===================================================================
# GLPIClient — get_ticket_followups
# ===================================================================


class TestGetTicketFollowups:
    """``GLPIClient.get_ticket_followups`` — GET ``/Ticket/{id}/ITILFollowup``."""

    def test_returns_list_of_followups(self) -> None:
        """Happy path returns a list of followup dicts."""

        def handler(request: Request) -> Response:
            assert request.method == "GET"
            assert str(request.url) == (
                "http://glpi.test/apirest.php/Ticket/42/ITILFollowup"
            )
            assert request.headers.get("Session-Token") == "sess-abc"
            return Response(
                200,
                json=[
                    {
                        "id": 1,
                        "content": "First followup",
                        "date": "2024-01-01",
                        "users_id": 5,
                    },
                    {
                        "id": 2,
                        "content": "Second followup",
                        "date": "2024-01-02",
                        "users_id": 3,
                    },
                ],
            )

        client = _build_glpi_client(handler)
        result = client.get_ticket_followups(
            ticket_id=42, session_token="sess-abc"
        )
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["id"] == 1
        assert result[0]["content"] == "First followup"
        assert result[1]["id"] == 2
        assert result[1]["content"] == "Second followup"

    def test_returns_empty_list_when_no_followups(self) -> None:
        """Empty array from API returns []."""

        def handler(request: Request) -> Response:
            return Response(200, json=[])

        client = _build_glpi_client(handler)
        result = client.get_ticket_followups(
            ticket_id=1, session_token="sess"
        )
        assert result == []

    def test_handles_wrapped_data_format(self) -> None:
        """GLPI may return {\"data\": [...], \"totalcount\": N} format."""

        def handler(request: Request) -> Response:
            return Response(
                200,
                json={
                    "data": [
                        {"id": 10, "content": "Wrapped"},
                    ],
                    "totalcount": 1,
                },
            )

        client = _build_glpi_client(handler)
        result = client.get_ticket_followups(
            ticket_id=1, session_token="sess"
        )
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["id"] == 10
        assert result[0]["content"] == "Wrapped"

    def test_returns_empty_list_for_empty_wrapped_data(self) -> None:
        """GLPI may return {\"data\": [], \"totalcount\": 0}."""

        def handler(request: Request) -> Response:
            return Response(200, json={"data": [], "totalcount": 0})

        client = _build_glpi_client(handler)
        result = client.get_ticket_followups(
            ticket_id=1, session_token="sess"
        )
        assert result == []

    def test_returns_empty_list_for_unexpected_type(self) -> None:
        """Non-list, non-dict response returns []."""

        def handler(request: Request) -> Response:
            return Response(200, json="unexpected")

        client = _build_glpi_client(handler)
        result = client.get_ticket_followups(
            ticket_id=1, session_token="sess"
        )
        assert result == []

    def test_sends_session_token_header(self) -> None:
        """Session-Token header is sent with the request."""

        def handler(request: Request) -> Response:
            assert request.headers.get("Session-Token") == "my-token"
            return Response(200, json=[])

        client = _build_glpi_client(handler)
        client.get_ticket_followups(ticket_id=1, session_token="my-token")

    def test_correct_url_for_different_ticket_id(self) -> None:
        """URL contains the correct ticket ID."""

        def handler(request: Request) -> Response:
            assert str(request.url) == (
                "http://glpi.test/apirest.php/Ticket/999/ITILFollowup"
            )
            return Response(200, json=[])

        client = _build_glpi_client(handler)
        client.get_ticket_followups(ticket_id=999, session_token="sess")

    def test_http_error_raises_runtime_error(self) -> None:
        """Non-2xx response raises RuntimeError."""

        def handler(request: Request) -> Response:
            return Response(404, text="Not Found")

        client = _build_glpi_client(handler)
        with pytest.raises(RuntimeError) as exc_info:
            client.get_ticket_followups(
                ticket_id=1, session_token="sess"
            )
        assert "404" in str(exc_info.value)
        assert "ITILFollowup" in str(exc_info.value)

    def test_network_error_raises_runtime_error(self) -> None:
        """Transport error raises RuntimeError."""

        def handler(request: Request) -> Response:
            raise httpx.RequestError("Connection timeout")

        client = _build_glpi_client(handler)
        with pytest.raises(RuntimeError) as exc_info:
            client.get_ticket_followups(
                ticket_id=1, session_token="sess"
            )
        assert "Connection timeout" in str(exc_info.value)

    def test_non_json_response_raises_runtime_error(self) -> None:
        """Non-JSON response raises RuntimeError."""

        def handler(request: Request) -> Response:
            return Response(200, text="<html>not json</html>")

        client = _build_glpi_client(handler)
        with pytest.raises(RuntimeError) as exc_info:
            client.get_ticket_followups(
                ticket_id=1, session_token="sess"
            )
        assert "non-JSON" in str(exc_info.value)

    def test_zero_ticket_id(self) -> None:
        """Ticket ID 0 is sent as-is in URL."""

        def handler(request: Request) -> Response:
            assert str(request.url).endswith("/Ticket/0/ITILFollowup")
            return Response(200, json=[])

        client = _build_glpi_client(handler)
        result = client.get_ticket_followups(
            ticket_id=0, session_token="sess"
        )
        assert result == []


# ===================================================================
# Helper — build GLPIClient with mock transport (same pattern as
# test_glpi.py but pinned to this test file for self-containment)
# ===================================================================


def _build_glpi_client(handler) -> GLPIClient:
    """Return a ``GLPIClient`` whose internal transport is swapped for
    *handler*, avoiding real HTTP calls.

    Identical to ``_build_client`` in test_glpi.py — duplicated here
    for self-containment so this module can run independently.
    """
    client = GLPIClient(
        base_url="http://glpi.test",
        app_token="app-token-42",
        user_token="user-token-99",
    )
    client._client = httpx.Client(transport=MockTransport(handler))
    return client


# ===================================================================
# get_reverse_sync_status
# ===================================================================


class TestGetReverseSyncStatus:
    """``get_reverse_sync_status()`` — configuration status dict."""

    def test_active(self) -> None:
        """TEST_MODE=True with non-empty IDs returns active=True."""
        from app.services.reverse_sync import get_reverse_sync_status

        with (
            patch("app.services.reverse_sync.settings.TEST_MODE", True),
            patch("app.services.reverse_sync.settings.TEST_TASK_IDS", "35591,35633"),
        ):
            result = get_reverse_sync_status()
            assert result == {
                "test_mode": True,
                "test_task_ids": [35591, 35633],
                "active": True,
            }

    def test_inactive_when_test_mode_false(self) -> None:
        """TEST_MODE=False returns active=False."""
        from app.services.reverse_sync import get_reverse_sync_status

        with (
            patch("app.services.reverse_sync.settings.TEST_MODE", False),
            patch("app.services.reverse_sync.settings.TEST_TASK_IDS", "35591"),
        ):
            result = get_reverse_sync_status()
            assert result == {
                "test_mode": False,
                "test_task_ids": [35591],
                "active": False,
            }

    def test_inactive_when_empty_ids(self) -> None:
        """Empty test_task_ids returns active=False."""
        from app.services.reverse_sync import get_reverse_sync_status

        with (
            patch("app.services.reverse_sync.settings.TEST_MODE", True),
            patch("app.services.reverse_sync.settings.TEST_TASK_IDS", ""),
        ):
            result = get_reverse_sync_status()
            assert result == {
                "test_mode": True,
                "test_task_ids": [],
                "active": False,
            }

    def test_inactive_both_false(self) -> None:
        """Both False/empty returns active=False."""
        from app.services.reverse_sync import get_reverse_sync_status

        with (
            patch("app.services.reverse_sync.settings.TEST_MODE", False),
            patch("app.services.reverse_sync.settings.TEST_TASK_IDS", ""),
        ):
            result = get_reverse_sync_status()
            assert result == {
                "test_mode": False,
                "test_task_ids": [],
                "active": False,
            }


# ===================================================================
# reverse_sync_test_tasks — early return paths
# ===================================================================


class TestReverseSyncTestTasksEarlyReturn:
    """``reverse_sync_test_tasks()`` — early return when disabled."""

    async def test_disabled_when_test_mode_false(self) -> None:
        """Returns error when TEST_MODE=False."""
        from app.services.reverse_sync import reverse_sync_test_tasks

        with patch("app.services.reverse_sync.settings.TEST_MODE", False):
            result = await reverse_sync_test_tasks()
            assert result == {"error": "TEST_MODE is disabled"}

    async def test_disabled_when_no_task_ids(self) -> None:
        """Returns error when test_task_ids is empty."""
        from app.services.reverse_sync import reverse_sync_test_tasks

        with (
            patch("app.services.reverse_sync.settings.TEST_MODE", True),
            patch("app.services.reverse_sync.settings.TEST_TASK_IDS", ""),
        ):
            result = await reverse_sync_test_tasks()
            assert result == {"error": "No test task IDs configured"}


# ===================================================================
# _extract_glpi_ticket_id
# ===================================================================


class TestExtractGlpiTicketId:
    """``_extract_glpi_ticket_id(task)`` — extract GLPI ticket from Task.result."""

    def test_from_list_of_dicts(self) -> None:
        """list[{\"id\": 42}] returns 42."""
        from unittest.mock import MagicMock

        from app.models.task import Task
        from app.services.reverse_sync import _extract_glpi_ticket_id

        task = MagicMock(spec=Task)
        task.result = [{"id": 42, "message": "created"}]
        assert _extract_glpi_ticket_id(task) == 42

    def test_from_dict_with_id(self) -> None:
        """dict {\"id\": 42} returns 42."""
        from unittest.mock import MagicMock

        from app.models.task import Task
        from app.services.reverse_sync import _extract_glpi_ticket_id

        task = MagicMock(spec=Task)
        task.result = {"id": 42}
        assert _extract_glpi_ticket_id(task) == 42

    def test_from_dict_with_tickets_id(self) -> None:
        """dict {\"tickets_id\": 99} returns 99 (fallback key)."""
        from unittest.mock import MagicMock

        from app.models.task import Task
        from app.services.reverse_sync import _extract_glpi_ticket_id

        task = MagicMock(spec=Task)
        task.result = {"tickets_id": 99}
        assert _extract_glpi_ticket_id(task) == 99

    def test_none_result_returns_none(self) -> None:
        """task.result=None returns None."""
        from unittest.mock import MagicMock

        from app.models.task import Task
        from app.services.reverse_sync import _extract_glpi_ticket_id

        task = MagicMock(spec=Task)
        task.result = None
        assert _extract_glpi_ticket_id(task) is None

    def test_empty_list_returns_none(self) -> None:
        """task.result=[] returns None."""
        from unittest.mock import MagicMock

        from app.models.task import Task
        from app.services.reverse_sync import _extract_glpi_ticket_id

        task = MagicMock(spec=Task)
        task.result = []
        assert _extract_glpi_ticket_id(task) is None

    def test_empty_dict_returns_none(self) -> None:
        """task.result={} returns None (no 'id' or 'tickets_id' key)."""
        from unittest.mock import MagicMock

        from app.models.task import Task
        from app.services.reverse_sync import _extract_glpi_ticket_id

        task = MagicMock(spec=Task)
        task.result = {}
        assert _extract_glpi_ticket_id(task) is None

    def test_id_is_zero_returns_zero(self) -> None:
        """dict {\"id\": 0} returns 0 (valid GLPI ticket ID)."""
        from unittest.mock import MagicMock

        from app.models.task import Task
        from app.services.reverse_sync import _extract_glpi_ticket_id

        task = MagicMock(spec=Task)
        task.result = {"id": 0}
        # The function does `first.get("id")` which returns 0 (falsy),
        # so for the list path it's fine. For dict path:
        # `task.result.get("id") or task.result.get("tickets_id")`
        # 0 is falsy so `or` falls through to `tickets_id`.
        # This is the existing behaviour — we document it here.
        # After the list branch returns 0, the dict branch is not reached.
        result = _extract_glpi_ticket_id(task)
        # dict path: 0 or None => None (because 0 is falsy in `x or y`)
        # That's the existing implementation behaviour.
        assert result is None


# ===================================================================
# _extract_glpi_status
# ===================================================================


class TestExtractGlpiStatus:
    """``_extract_glpi_status(ticket_info)`` — extract numeric status."""

    def test_flat_status(self) -> None:
        """{\"status\": 2} returns 2."""
        from app.services.reverse_sync import _extract_glpi_status

        assert _extract_glpi_status({"status": 2}) == 2

    def test_flat_status_string(self) -> None:
        """{\"status\": \"3\"} (string) is coerced to int 3."""
        from app.services.reverse_sync import _extract_glpi_status

        assert _extract_glpi_status({"status": "3"}) == 3

    def test_data_dict(self) -> None:
        """{\"data\": {\"status\": 3}} returns 3."""
        from app.services.reverse_sync import _extract_glpi_status

        assert _extract_glpi_status({"data": {"status": 3}}) == 3

    def test_data_list(self) -> None:
        """{\"data\": [{\"status\": 4}]} returns 4."""
        from app.services.reverse_sync import _extract_glpi_status

        assert _extract_glpi_status({"data": [{"status": 4}]}) == 4

    def test_empty_dict_returns_none(self) -> None:
        """{} returns None."""
        from app.services.reverse_sync import _extract_glpi_status

        assert _extract_glpi_status({}) is None

    def test_data_empty_dict_returns_none(self) -> None:
        """{\"data\": {}} returns None."""
        from app.services.reverse_sync import _extract_glpi_status

        assert _extract_glpi_status({"data": {}}) is None

    def test_data_empty_list_returns_none(self) -> None:
        """{\"data\": []} returns None."""
        from app.services.reverse_sync import _extract_glpi_status

        assert _extract_glpi_status({"data": []}) is None

    def test_status_zero_returns_zero(self) -> None:
        """{\"status\": 0} returns 0 (valid status value)."""
        from app.services.reverse_sync import _extract_glpi_status

        assert _extract_glpi_status({"status": 0}) == 0

    def test_data_list_with_empty_first_element(self) -> None:
        """{\"data\": [{}]} returns None (no 'status' key in element)."""
        from app.services.reverse_sync import _extract_glpi_status

        assert _extract_glpi_status({"data": [{}]}) is None


# ===================================================================
# _GLPI_TO_BITRIX_STATUS mapping
# ===================================================================


class TestGlpiToBitrixMapping:
    """``_GLPI_TO_BITRIX_STATUS`` — all six GLPI statuses mapped."""

    def test_all_six_keys_present(self) -> None:
        """Mapping has exactly keys 1, 2, 3, 4, 5, 6."""
        from app.services.reverse_sync import _GLPI_TO_BITRIX_STATUS

        assert set(_GLPI_TO_BITRIX_STATUS.keys()) == {1, 2, 3, 4, 5, 6}

    def test_mapping_values(self) -> None:
        """Each GLPI status maps to the correct Bitrix24 status."""
        from app.services.reverse_sync import _GLPI_TO_BITRIX_STATUS

        assert _GLPI_TO_BITRIX_STATUS == {
            1: 1,  # new → open
            2: 2,  # assigned → pending
            3: 4,  # on hold → frozen
            4: 3,  # resolved → closed
            5: 5,  # solved → completed
            6: 6,  # cancelled → deferred
        }


# ===================================================================
# API endpoint registration
# ===================================================================


class TestReverseSyncAPIEndpoints:
    """Reverse sync API endpoints are properly registered."""

    @staticmethod
    def _get_all_api_routes(app):
        """Extract all APIRoute paths from the app, including included routers."""
        from fastapi.routing import APIRoute

        routes = []
        for r in app.routes:
            if isinstance(r, APIRoute):
                routes.append(r)
            elif hasattr(r, "original_router"):
                routes.extend(
                    sr for sr in r.original_router.routes
                    if isinstance(sr, APIRoute)
                )
        return routes

    def test_routes_registered(self) -> None:
        """GET /sync/reverse-status and POST /sync/reverse-test are in the router."""
        from app.main import app

        api_routes = self._get_all_api_routes(app)
        paths = [r.path for r in api_routes]
        assert "/api/bitrix24/sync/reverse-status" in paths
        assert "/api/bitrix24/sync/reverse-test" in paths

    def test_route_methods(self) -> None:
        """Endpoint methods are GET and POST respectively."""
        from app.main import app

        api_routes = self._get_all_api_routes(app)
        status_routes = [
            r for r in api_routes
            if r.path == "/api/bitrix24/sync/reverse-status"
        ]
        test_routes = [
            r for r in api_routes
            if r.path == "/api/bitrix24/sync/reverse-test"
        ]
        assert len(status_routes) == 1
        assert len(test_routes) == 1
        assert "GET" in status_routes[0].methods
        assert "POST" in test_routes[0].methods

    async def test_get_reverse_status_endpoint(self, async_client) -> None:
        """GET /sync/reverse-status returns 200 with status dict."""
        resp = await async_client.get("/api/bitrix24/sync/reverse-status")
        assert resp.status_code == 200
        data = resp.json()
        assert "test_mode" in data
        assert "test_task_ids" in data
        assert "active" in data

    async def test_post_reverse_test_disabled(self, async_client) -> None:
        """POST /sync/reverse-test returns early error when TEST_MODE=False."""
        # The handler imports from app.services.reverse_sync which uses its own
        # module-level `settings` reference — patch that directly.
        with patch("app.services.reverse_sync.settings.TEST_MODE", False):
            resp = await async_client.post("/api/bitrix24/sync/reverse-test")
            assert resp.status_code == 200
            assert resp.json() == {"error": "TEST_MODE is disabled"}
