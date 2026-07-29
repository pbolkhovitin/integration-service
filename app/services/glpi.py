"""Synchronous httpx-based GLPI API client (App-Token auth — Phase 1 MVP)."""

import json
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class GLPIClient:
    """Synchronous httpx-based client for GLPI API (v1 MVP — App-Token auth).

    Wraps an :class:`httpx.Client` with GLPI-specific headers and authentication.
    Can be used as a context manager::

        with GLPIClient(url, app_token, user_token) as client:
            session = client.init_session()
            ticket = client.show_ticket(123)

    Or closed manually::

        client = GLPIClient(url, app_token, user_token)
        try:
            session = client.init_session()
            ...
        finally:
            client.close()
    """

    def __init__(
        self,
        base_url: str,
        app_token: str,
        user_token: str,
        timeout: int = 30,
    ) -> None:
        """Initialize the GLPI API client.

        Args:
            base_url: GLPI server URL (e.g. ``http://glpi:80``).
            app_token: GLPI *App-Token* (from settings).
            user_token: GLPI *API User Token* (from settings).
            timeout: Request timeout in seconds (default 30).
        """
        self._base_url = base_url.rstrip("/")
        self._app_token = app_token
        self._user_token = user_token
        self._timeout = timeout

        self._client = httpx.Client(
            headers={
                "Content-Type": "application/json",
                "App-Token": self._app_token,
            },
            timeout=httpx.Timeout(self._timeout),
        )

    def close(self) -> None:
        """Close the underlying HTTP client and free resources."""
        self._client.close()

    def __enter__(self) -> "GLPIClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def init_session(self) -> str:
        """Initialise a GLPI API session and return the session token.

        Sends a ``GET {base_url}/apirest.php/initSession?user_token=…``
        request with the App-Token header. GLPI legacy API uses
        ``user_token`` as query param, NOT Basic Auth.

        Returns:
            Session token string from the ``session_token`` field of the
            JSON response.

        Raises:
            RuntimeError: On HTTP failure or missing *session_token* in the
                response payload.
        """
        url = f"{self._base_url}/apirest.php/initSession"
        params = {"user_token": self._user_token}
        logger.debug("GET %s — initialising GLPI session", url)

        return self._call(
            method="GET", url=url, params=params, extract_key="session_token"
        )

    # ------------------------------------------------------------------
    # Ticket operations
    # ------------------------------------------------------------------

    def show_ticket(
        self,
        ticket_id: int,
        session_token: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve a GLPI ticket by its numeric ID.

        Sends a ``GET {base_url}/apirest.php/Ticket/{ticket_id}`` request.

        Args:
            ticket_id: Numeric ticket identifier.
            session_token: Optional session token for authentication. When
                provided, sets the ``Session-Token`` header.

        Returns:
            Parsed JSON response as a dictionary.

        Raises:
            RuntimeError: On HTTP failure.
        """
        url = f"{self._base_url}/apirest.php/Ticket/{ticket_id}"
        logger.debug("GET %s — fetching ticket %d", url, ticket_id)

        return self._call(method="GET", url=url, session_token=session_token)

    def create_ticket(
        self,
        name: str,
        content: str,
        session_token: str,
    ) -> dict[str, Any]:
        """Create a new GLPI incident ticket.

        Sends a ``POST {base_url}/apirest.php/Ticket`` request with
        ``{"input": [{"name": …, "content": …, "type": 1}]}`` as the
        JSON payload (``type: 1`` = Incident).

        Args:
            name: Short title of the ticket.
            content: Body / description of the ticket.
            session_token: A valid session token obtained from
                :meth:`init_session`.

        Returns:
            Parsed JSON response as a dictionary.

        Raises:
            RuntimeError: On HTTP failure.
        """
        url = f"{self._base_url}/apirest.php/Ticket"
        payload: dict[str, list[dict[str, Any]]] = {
            "input": [
                {
                    "name": name,
                    "content": content,
                    "type": 1,
                }
            ]
        }
        logger.debug("POST %s — creating ticket %r", url, name)

        return self._call(
            method="POST",
            url=url,
            json_body=payload,
            session_token=session_token,
        )

    # ------------------------------------------------------------------
    # Ticket update
    # ------------------------------------------------------------------

    def update_ticket(
        self,
        ticket_id: int,
        session_token: str,
        **fields: Any,
    ) -> dict[str, Any]:
        """Update an existing GLPI ticket.

        Sends a ``PUT {base_url}/apirest.php/Ticket/{ticket_id}`` request.

        Args:
            ticket_id: Numeric ticket identifier.
            session_token: A valid session token.
            **fields: Fields to update (e.g. ``status=5``).

        Returns:
            Parsed JSON response as a dictionary.

        Raises:
            RuntimeError: On HTTP failure.
        """
        url = f"{self._base_url}/apirest.php/Ticket/{ticket_id}"
        payload: dict[str, dict[str, Any]] = {"input": fields}
        logger.debug("PUT %s — updating ticket %d: %s", url, ticket_id, fields)

        return self._call(
            method="PUT",
            url=url,
            json_body=payload,
            session_token=session_token,
        )

    # ------------------------------------------------------------------
    # Ticket followups
    # ------------------------------------------------------------------

    def get_ticket_followups(
        self,
        ticket_id: int,
        session_token: str,
    ) -> list[dict[str, Any]]:
        """Get ITIL followups for a GLPI ticket.

        Sends a ``GET {base_url}/apirest.php/Ticket/{ticket_id}/ITILFollowup`` request.

        Args:
            ticket_id: Numeric ticket identifier.
            session_token: A valid session token.

        Returns:
            List of followup dicts, each with ``id``, ``content``,
            ``date``, ``users_id``, etc.

        Raises:
            RuntimeError: On HTTP failure.
        """
        url = f"{self._base_url}/apirest.php/Ticket/{ticket_id}/ITILFollowup"
        logger.debug("GET %s — fetching followups for ticket %d", url, ticket_id)

        result = self._call(
            method="GET", url=url, session_token=session_token
        )
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            # GLPI may return {"data": [...], "totalcount": N}
            return result.get("data", [])
        return []

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _call(
        self,
        method: str,
        url: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        extract_key: str | None = None,
        session_token: str | None = None,
    ) -> Any:
        """Low-level request helper with consistent error handling.

        Args:
            method: HTTP method (``GET``, ``POST``, …).
            url: Full request URL.
            json_body: Optional JSON-serialisable request body.
            extract_key: If given, the response JSON is expected to
                contain this key and its value is returned instead of
                the whole dict.
            session_token: When provided, sets the ``Session-Token``
                header for the request.

        Returns:
            Response JSON parsed into Python objects — either the full
            dictionary or the value at *extract_key*.

        Raises:
            RuntimeError: On HTTP error (non-2xx) or when *extract_key*
                is missing from the response payload.
        """
        headers = {}
        if session_token is not None:
            headers["Session-Token"] = session_token

        try:
            response = self._client.request(
                method=method,
                url=url,
                headers=headers or None,
                params=params,
                json=json_body,
            )
        except httpx.RequestError as exc:
            raise RuntimeError(
                f"GLPI request failed: {method} {url} — {exc}"
            ) from exc

        if not response.is_success:
            status = response.status_code
            detail = _summarise_body(response.text)
            raise RuntimeError(
                f"GLPI returned HTTP {status} for {method} {url}"
                + (f": {detail}" if detail else "")
            )

        try:
            data = response.json()
        except (json.JSONDecodeError, httpx.DecodingError) as exc:
            raise RuntimeError(
                f"GLPI returned non-JSON response for {method} {url}: {exc}"
            ) from exc

        if extract_key is not None:
            if extract_key not in data:
                raise RuntimeError(
                    f"GLPI response for {method} {url} is missing "
                    f"required field {extract_key!r}"
                )
            return data[extract_key]

        return data


def _summarise_body(body: str, max_len: int = 200) -> str:
    """Return a short excerpt of *body* for error messages."""
    if not body or not body.strip():
        return ""
    stripped = body.strip()
    if len(stripped) <= max_len:
        return stripped
    return stripped[:max_len] + "…"
