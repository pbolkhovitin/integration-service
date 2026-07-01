"""Tests for app.services.glpi — GLPIClient (synchronous httpx wrapper).

All mocking is at the transport layer via ``httpx.MockTransport`` (no real HTTP).
"""

from __future__ import annotations

import httpx
import json
import pytest
from httpx import MockTransport, Request, Response
from typing import Any

from app.services.glpi import GLPIClient, _summarise_body

# ===================================================================
# Fixtures
# ===================================================================


def _build_client(handler) -> GLPIClient:
    """Return a ``GLPIClient`` whose internal transport is swapped for
    *handler*, avoiding real HTTP calls.

    The handler receives an ``httpx.Request`` and must return an
    ``httpx.Response`` (or raise ``httpx.RequestError`` for network
    failure simulation).
    """
    client = GLPIClient(
        base_url="http://glpi.test",
        app_token="app-token-42",
        user_token="user-token-99",
    )
    # Swap the transport — the only layer we mock
    client._client = httpx.Client(transport=MockTransport(handler))
    return client


# ===================================================================
# _summarise_body — module-level helper (pure function, no mock needed)
# ===================================================================


class TestSummariseBody:
    """_summarise_body edge cases."""

    def test_empty_string(self) -> None:
        assert _summarise_body("") == ""

    def test_whitespace_only(self) -> None:
        assert _summarise_body("   \n  \t  ") == ""

    def test_none_string_error_body_not_allowed(self) -> None:
        """Non-string body should raise AttributeError (no .strip())."""
        with pytest.raises(AttributeError):
            _summarise_body(None)  # type: ignore[arg-type]

    def test_short_body(self) -> None:
        assert _summarise_body("short") == "short"

    def test_body_equals_max_len(self) -> None:
        body = "a" * 200
        assert _summarise_body(body) == body

    def test_body_one_over_max_len(self) -> None:
        body = "a" * 201
        result = _summarise_body(body)
        assert result == "a" * 200 + "…"
        assert len(result) == 201

    def test_long_body_truncated(self) -> None:
        body = "x" * 500
        result = _summarise_body(body)
        assert result == "x" * 200 + "…"
        assert len(result) == 201

    def test_strips_trailing_whitespace_before_truncation(self) -> None:
        body = "  " + "y" * 200 + "\n\n"
        result = _summarise_body(body)
        assert result == "y" * 200
        assert len(result) == 200

    def test_strips_leading_whitespace_before_truncation(self) -> None:
        body = "\t\n" + "z" * 200 + " "
        result = _summarise_body(body)
        assert result == "z" * 200

    def test_custom_max_len(self) -> None:
        body = "hello world"
        assert _summarise_body(body, max_len=5) == "hello…"


# ===================================================================
# init_session
# ===================================================================


class TestInitSession:
    """POST /apirest.php/initSession."""

    def test_success_returns_session_token(self) -> None:
        def handler(request: Request) -> Response:
            assert request.method == "POST"
            assert (
                str(request.url)
                == "http://glpi.test/apirest.php/initSession"
            )
            return Response(200, json={"session_token": "tok-abc-123"})

        client = _build_client(handler)
        token = client.init_session()
        assert token == "tok-abc-123"

    def test_sends_authorization_header_with_user_token(self) -> None:
        """The httpx.Client is configured with BasicAuth; verify the
        request carries the expected ``Authorization`` header."""
        def handler(request: Request) -> Response:
            auth = request.headers.get("Authorization", "")
            assert "user-token-99" in auth, (
                f"Expected user-token-99 in Authorization header, got: {auth}"
            )
            assert auth.startswith("Basic "), (
                f"Expected Basic auth, got: {auth}"
            )
            return Response(200, json={"session_token": "tok"})

        client = _build_client(handler)
        token = client.init_session()
        assert token == "tok"

    def test_sends_app_token_header(self) -> None:
        def handler(request: Request) -> Response:
            assert request.headers.get("App-Token") == "app-token-42"
            return Response(200, json={"session_token": "tok"})

        client = _build_client(handler)
        token = client.init_session()
        assert token == "tok"

    def test_sends_content_type_header(self) -> None:
        def handler(request: Request) -> Response:
            ct = request.headers.get("Content-Type", "")
            assert "application/json" in ct
            return Response(200, json={"session_token": "tok"})

        client = _build_client(handler)
        client.init_session()

    def test_http_401_raises_runtime_error(self) -> None:
        def handler(request: Request) -> Response:
            return Response(401, text="Unauthorized")

        client = _build_client(handler)
        with pytest.raises(RuntimeError) as exc_info:
            client.init_session()
        msg = str(exc_info.value)
        assert "401" in msg
        assert "initSession" in msg

    def test_http_500_raises_runtime_error(self) -> None:
        def handler(request: Request) -> Response:
            return Response(500, text="Internal Server Error")

        client = _build_client(handler)
        with pytest.raises(RuntimeError) as exc_info:
            client.init_session()
        msg = str(exc_info.value)
        assert "500" in msg
        assert "Internal Server Error" in msg

    def test_missing_session_token_raises_runtime_error(self) -> None:
        def handler(request: Request) -> Response:
            return Response(
                200, json={"something_else": "not-a-token"}
            )

        client = _build_client(handler)
        with pytest.raises(RuntimeError) as exc_info:
            client.init_session()
        msg = str(exc_info.value)
        assert "session_token" in msg

    def test_network_error_raises_runtime_error(self) -> None:
        def handler(request: Request) -> Response:
            raise httpx.RequestError("Connection refused")

        client = _build_client(handler)
        with pytest.raises(RuntimeError) as exc_info:
            client.init_session()
        msg = str(exc_info.value)
        assert "Connection refused" in msg

    def test_non_json_response_raises_runtime_error(self) -> None:
        def handler(request: Request) -> Response:
            return Response(200, text="not-json-at-all")

        client = _build_client(handler)
        with pytest.raises(RuntimeError) as exc_info:
            client.init_session()
        msg = str(exc_info.value)
        assert "non-JSON" in msg

    def test_empty_json_response_no_session_token_raises(self) -> None:
        def handler(request: Request) -> Response:
            return Response(200, json={})

        client = _build_client(handler)
        with pytest.raises(RuntimeError) as exc_info:
            client.init_session()
        msg = str(exc_info.value)
        assert "session_token" in msg


# ===================================================================
# show_ticket
# ===================================================================


class TestShowTicket:
    """GET /apirest.php/Ticket/{id}."""

    def test_success_returns_ticket_dict(self) -> None:
        def handler(request: Request) -> Response:
            assert request.method == "GET"
            assert (
                str(request.url)
                == "http://glpi.test/apirest.php/Ticket/42"
            )
            return Response(
                200,
                json={
                    "id": 42,
                    "name": "Network Down",
                    "status": 2,
                    "content": "Office network unreachable",
                },
            )

        client = _build_client(handler)
        ticket = client.show_ticket(42)
        assert ticket == {
            "id": 42,
            "name": "Network Down",
            "status": 2,
            "content": "Office network unreachable",
        }

    def test_sends_no_session_token_header(self) -> None:
        """show_ticket should NOT send Session-Token (it's a GET)."""
        def handler(request: Request) -> Response:
            assert "Session-Token" not in request.headers
            return Response(200, json={"id": 1})

        client = _build_client(handler)
        client.show_ticket(1)

    def test_http_404_raises_runtime_error(self) -> None:
        def handler(request: Request) -> Response:
            return Response(404, text="Not Found")

        client = _build_client(handler)
        with pytest.raises(RuntimeError) as exc_info:
            client.show_ticket(999)
        msg = str(exc_info.value)
        assert "404" in msg
        assert "Not Found" in msg
        assert "Ticket/999" in msg

    def test_http_403_raises_runtime_error(self) -> None:
        def handler(request: Request) -> Response:
            return Response(403, text="Forbidden")

        client = _build_client(handler)
        with pytest.raises(RuntimeError) as exc_info:
            client.show_ticket(1)
        msg = str(exc_info.value)
        assert "403" in msg

    def test_network_error_raises_runtime_error(self) -> None:
        def handler(request: Request) -> Response:
            raise httpx.RequestError("Connection timeout")

        client = _build_client(handler)
        with pytest.raises(RuntimeError) as exc_info:
            client.show_ticket(1)
        msg = str(exc_info.value)
        assert "Connection timeout" in msg

    def test_non_json_response_raises_runtime_error(self) -> None:
        def handler(request: Request) -> Response:
            return Response(200, text="<html><body>Error</body></html>")

        client = _build_client(handler)
        with pytest.raises(RuntimeError) as exc_info:
            client.show_ticket(1)
        msg = str(exc_info.value)
        assert "non-JSON" in msg

    def test_http_error_with_empty_body_shows_no_detail(self) -> None:
        def handler(request: Request) -> Response:
            return Response(500, text="")

        client = _build_client(handler)
        with pytest.raises(RuntimeError) as exc_info:
            client.show_ticket(1)
        msg = str(exc_info.value)
        assert "500" in msg
        # No colon+detail suffix when body is empty
        assert ":" not in msg.rpartition("500")[2].strip() or not any(
            c in msg.partition("500")[2] for c in [":", "—", "–"]
        )


# ===================================================================
# create_ticket
# ===================================================================


class TestCreateTicket:
    """POST /apirest.php/Ticket — creates an incident ticket."""

    def test_success_returns_created_ticket(self) -> None:
        def handler(request: Request) -> Response:
            assert request.method == "POST"
            assert (
                str(request.url)
                == "http://glpi.test/apirest.php/Ticket"
            )

            # Verify the request payload
            body = json.loads(request.read())
            assert body == {
                "input": [
                    {
                        "name": "Printer not working",
                        "content": "The printer in room 42 is jammed",
                        "type": 1,
                    }
                ]
            }
            # Verify session token is sent
            assert request.headers.get("Session-Token") == "session-abc"

            return Response(
                201,
                json={"id": 101, "message": "Ticket created"},
            )

        client = _build_client(handler)
        result = client.create_ticket(
            name="Printer not working",
            content="The printer in room 42 is jammed",
            session_token="session-abc",
        )
        assert result == {"id": 101, "message": "Ticket created"}

    def test_sends_session_token_header(self) -> None:
        def handler(request: Request) -> Response:
            assert request.headers.get("Session-Token") == "sess-xyz"
            return Response(201, json={"id": 1})

        client = _build_client(handler)
        client.create_ticket("Name", "Content", "sess-xyz")

    def test_http_400_raises_runtime_error(self) -> None:
        def handler(request: Request) -> Response:
            return Response(400, text="Bad Request: missing fields")

        client = _build_client(handler)
        with pytest.raises(RuntimeError) as exc_info:
            client.create_ticket("n", "c", "s")
        msg = str(exc_info.value)
        assert "400" in msg
        assert "Bad Request" in msg

    def test_network_error_raises_runtime_error(self) -> None:
        def handler(request: Request) -> Response:
            raise httpx.RequestError("Connection reset by peer")

        client = _build_client(handler)
        with pytest.raises(RuntimeError) as exc_info:
            client.create_ticket("n", "c", "s")
        msg = str(exc_info.value)
        assert "Connection reset" in msg

    def test_non_json_response_raises_runtime_error(self) -> None:
        def handler(request: Request) -> Response:
            return Response(201, text="plain text, not json")

        client = _build_client(handler)
        with pytest.raises(RuntimeError) as exc_info:
            client.create_ticket("n", "c", "s")
        msg = str(exc_info.value)
        assert "non-JSON" in msg


# ===================================================================
# Context manager
# ===================================================================


class TestContextManager:
    """GLPIClient supports ``with`` statement."""

    def test_enter_returns_self(self) -> None:
        def handler(_: Request) -> Response:
            return Response(200, json={"session_token": "tok"})

        client = _build_client(handler)
        with client as cm:
            assert cm is client

    def test_exit_closes_underlying_client(self) -> None:
        def handler(_: Request) -> Response:
            return Response(200, json={"session_token": "tok"})

        client = _build_client(handler)
        with client:
            pass  # block executed, now exit closes

        # After context exit, requests should fail
        with pytest.raises(RuntimeError):
            client.init_session()

    def test_exit_on_exception_still_closes_client(self) -> None:
        def handler(_: Request) -> Response:
            return Response(200, json={"session_token": "tok"})

        client = _build_client(handler)
        with pytest.raises(ValueError, match="boom"):
            with client:
                raise ValueError("boom")

        # Client closed even after exception
        with pytest.raises(RuntimeError):
            client.init_session()

    def test_exit_called_once_per_context(self) -> None:
        """Closing multiple context instances is safe."""
        def handler(_: Request) -> Response:
            return Response(200, json={"session_token": "tok"})

        client = _build_client(handler)
        with client:
            pass
        # Second call to close is idempotent
        client.close()

    def test_nested_contexts_error_handling(self) -> None:
        """Each call to __exit__ should close the client."""
        def handler(_: Request) -> Response:
            return Response(200, json={"session_token": "tok"})

        client = _build_client(handler)
        # Simulate what happens when __exit__ is called
        client.__exit__(None, None, None)
        with pytest.raises(RuntimeError):
            client.init_session()


# ===================================================================
# close — idempotency
# ===================================================================


class TestClose:
    """Client.close() behaviour."""

    def test_close_is_idempotent(self) -> None:
        def handler(_: Request) -> Response:
            return Response(200, json={"session_token": "tok"})

        client = _build_client(handler)
        client.close()
        # Second call MUST NOT raise
        client.close()
        client.close()  # third for good measure

    def test_after_close_requests_raise(self) -> None:
        def handler(_: Request) -> Response:
            return Response(200, json={"session_token": "tok"})

        client = _build_client(handler)
        client.close()
        with pytest.raises(RuntimeError):
            client.init_session()

    def test_close_and_context_manager_both_work(self) -> None:
        """Can close then use context (closed already — safe skip)."""
        def handler(_: Request) -> Response:
            return Response(200, json={"session_token": "tok"})

        client = _build_client(handler)
        client.close()
        # Opening a context on an already-closed client is safe
        with client:
            pass  # should not raise

    def test_close_does_not_affect_subsequent_operations_on_new_client(
        self,
    ) -> None:
        """Closing one client must not affect another."""
        def handler(request: Request) -> Response:
            return Response(200, json={"session_token": "tok"})

        client_a = _build_client(handler)
        client_b = _build_client(handler)

        client_a.close()
        # client_b should still work
        token = client_b.init_session()
        assert token == "tok"


# ===================================================================
# Integration-like: verify client construction and URL normalization
# ===================================================================


class TestClientConstruction:
    """GLPIClient __init__ behaviour not covered by transport tests."""

    def test_base_url_trailing_slash_stripped(self) -> None:
        """Trailing slash on base_url is stripped."""
        client = GLPIClient(
            base_url="http://glpi.test/",
            app_token="a",
            user_token="u",
        )
        assert client._base_url == "http://glpi.test"

    def test_base_url_no_trailing_slash_unchanged(self) -> None:
        client = GLPIClient(
            base_url="http://glpi.test",
            app_token="a",
            user_token="u",
        )
        assert client._base_url == "http://glpi.test"

    def test_default_timeout_is_30(self) -> None:
        client = GLPIClient(
            base_url="http://glpi.test",
            app_token="a",
            user_token="u",
        )
        assert client._timeout == 30

    def test_custom_timeout(self) -> None:
        client = GLPIClient(
            base_url="http://glpi.test",
            app_token="a",
            user_token="u",
            timeout=60,
        )
        assert client._timeout == 60

    def test_underlying_client_has_correct_headers(self) -> None:
        client = GLPIClient(
            base_url="http://glpi.test",
            app_token="my-app-token",
            user_token="my-user-token",
        )
        assert client._client.headers.get("App-Token") == "my-app-token"
        assert "application/json" in client._client.headers.get(
            "Content-Type", ""
        )


# ===================================================================
# Adversarial — show_ticket attack vectors
# ===================================================================


class TestAdversarialShowTicket:
    """Adversarial / attack-vector tests for show_ticket.

    Every test asserts that the client passes the malformed input
    through **as-is** — GLPIClient is a pass-through, not a sanitizer.
    """

    def test_path_traversal_in_ticket_id(self) -> None:
        """Path traversal string in ticket ID — httpx normalises away
        the ``../``, collapsing ``/Ticket/../../etc/passwd`` to
        ``/etc/passwd`` before the request is dispatched.
        """
        def handler(request: Request) -> Response:
            # httpx resolves ".." segments in the path before sending,
            # so the final path is /etc/passwd, not /Ticket/../../etc/passwd.
            assert str(request.url) == "http://glpi.test/etc/passwd"
            return Response(200, json={"id": 1})

        client = _build_client(handler)
        result = client.show_ticket("../../etc/passwd")  # type: ignore[arg-type]
        assert result == {"id": 1}

    def test_negative_ticket_id(self) -> None:
        """Negative ticket ID is sent as-is in the URL."""
        def handler(request: Request) -> Response:
            assert str(request.url).endswith("/-1")
            return Response(200, json={"id": -1})

        client = _build_client(handler)
        result = client.show_ticket(-1)
        assert result == {"id": -1}

    def test_zero_ticket_id(self) -> None:
        """Zero ticket ID is sent as-is in the URL."""
        def handler(request: Request) -> Response:
            assert str(request.url).endswith("/0")
            return Response(200, json={"id": 0})

        client = _build_client(handler)
        result = client.show_ticket(0)
        assert result == {"id": 0}

    def test_session_token_header_sent_when_provided(self) -> None:
        """Session-Token header IS present when session_token is passed."""
        def handler(request: Request) -> Response:
            assert request.headers.get("Session-Token") == "injected-token-999"
            return Response(200, json={"id": 1})

        client = _build_client(handler)
        result = client.show_ticket(1, session_token="injected-token-999")
        assert result == {"id": 1}

    def test_sql_injection_string_as_ticket_id(self) -> None:
        """SQL-looking string as ticket ID — httpx URL-encodes the
        space so ``1 OR 1=1`` becomes ``1%20OR%201=1`` in the path.
        """
        def handler(request: Request) -> Response:
            # httpx percent-encodes characters not allowed in URL paths
            assert "1%20OR%201=1" in str(request.url)
            return Response(200, json={"id": 1})

        client = _build_client(handler)
        result = client.show_ticket("1 OR 1=1")  # type: ignore[arg-type]
        assert result == {"id": 1}


# ===================================================================
# Adversarial — create_ticket attack vectors
# ===================================================================


class TestAdversarialCreateTicket:
    """Adversarial / attack-vector tests for create_ticket.

    All malformed payloads should pass through **as-is** — the client
    is a transparent HTTP wrapper, not a sanitisation layer.
    """

    def test_sql_injection_in_name(self) -> None:
        """SQL injection in ticket name passes through as-is."""
        name = "' OR 1=1 --"
        content = "Normal content"

        def handler(request: Request) -> Response:
            body = json.loads(request.read())
            assert body["input"][0]["name"] == "' OR 1=1 --"
            assert body["input"][0]["content"] == "Normal content"
            return Response(201, json={"id": 1})

        client = _build_client(handler)
        result = client.create_ticket(name, content, "sess")
        assert result == {"id": 1}

    def test_sql_injection_in_content(self) -> None:
        """SQL injection in ticket content passes through as-is."""
        name = "Test"
        content = "'; DROP TABLE tickets; --"

        def handler(request: Request) -> Response:
            body = json.loads(request.read())
            assert body["input"][0]["name"] == "Test"
            assert body["input"][0]["content"] == "'; DROP TABLE tickets; --"
            return Response(201, json={"id": 1})

        client = _build_client(handler)
        result = client.create_ticket(name, content, "sess")
        assert result == {"id": 1}

    def test_xss_in_content(self) -> None:
        """XSS payload in ticket content passes through as-is."""
        name = "Test Ticket"
        content = "<script>alert('xss')</script>"

        def handler(request: Request) -> Response:
            body = json.loads(request.read())
            assert body["input"][0]["content"] == (
                "<script>alert('xss')</script>"
            )
            return Response(201, json={"id": 1})

        client = _build_client(handler)
        result = client.create_ticket(name, content, "sess")
        assert result == {"id": 1}

    def test_extremely_long_name(self) -> None:
        """10 001‑character ticket name passes through as-is."""
        name = "A" * 10_001
        content = "Normal content"

        def handler(request: Request) -> Response:
            body = json.loads(request.read())
            assert body["input"][0]["name"] == name
            assert len(body["input"][0]["name"]) == 10_001
            return Response(201, json={"id": 1})

        client = _build_client(handler)
        result = client.create_ticket(name, content, "sess")
        assert result == {"id": 1}

    def test_extremely_long_content(self) -> None:
        """1 000 001‑character content passes through as-is."""
        name = "Test"
        content = "B" * 1_000_001

        def handler(request: Request) -> Response:
            body = json.loads(request.read())
            assert body["input"][0]["content"] == content
            assert len(body["input"][0]["content"]) == 1_000_001
            return Response(201, json={"id": 1})

        client = _build_client(handler)
        result = client.create_ticket(name, content, "sess")
        assert result == {"id": 1}

    def test_unicode_emoji_in_name(self) -> None:
        """Unicode / emoji characters in ticket name pass through as-is."""
        name = "🔥🔥🔥 Server on fire 🚒🧯"
        content = "Unicode test ✓ 日本語 русский"

        def handler(request: Request) -> Response:
            body = json.loads(request.read())
            assert body["input"][0]["name"] == (
                "🔥🔥🔥 Server on fire 🚒🧯"
            )
            assert body["input"][0]["content"] == (
                "Unicode test ✓ 日本語 русский"
            )
            return Response(201, json={"id": 1})

        client = _build_client(handler)
        result = client.create_ticket(name, content, "sess")
        assert result == {"id": 1}

    def test_empty_name(self) -> None:
        """Empty-string ticket name passes through as-is."""
        def handler(request: Request) -> Response:
            body = json.loads(request.read())
            assert body["input"][0]["name"] == ""
            assert body["input"][0]["content"] == "Content with empty name"
            return Response(201, json={"id": 1})

        client = _build_client(handler)
        result = client.create_ticket("", "Content with empty name", "sess")
        assert result == {"id": 1}

    def test_empty_content(self) -> None:
        """Empty-string ticket content passes through as-is."""
        def handler(request: Request) -> Response:
            body = json.loads(request.read())
            assert body["input"][0]["name"] == "Empty content test"
            assert body["input"][0]["content"] == ""
            return Response(201, json={"id": 1})

        client = _build_client(handler)
        result = client.create_ticket("Empty content test", "", "sess")
        assert result == {"id": 1}

    def test_none_name(self) -> None:
        """``None`` ticket name serialises to ``null`` in JSON body."""
        def handler(request: Request) -> Response:
            body = json.loads(request.read())
            assert body["input"][0]["name"] is None
            assert body["input"][0]["content"] == "Content with null name"
            return Response(201, json={"id": 1})

        client = _build_client(handler)
        result = client.create_ticket(
            None, "Content with null name", "sess"  # type: ignore[arg-type]
        )
        assert result == {"id": 1}

    def test_none_content(self) -> None:
        """``None`` ticket content serialises to ``null`` in JSON body."""
        def handler(request: Request) -> Response:
            body = json.loads(request.read())
            assert body["input"][0]["name"] == "Null content test"
            assert body["input"][0]["content"] is None
            return Response(201, json={"id": 1})

        client = _build_client(handler)
        result = client.create_ticket(
            "Null content test", None, "sess"  # type: ignore[arg-type]
        )
        assert result == {"id": 1}
