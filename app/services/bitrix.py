"""Synchronous httpx-based Bitrix24 REST API client for polling tasks."""

import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class BitrixClient:
    """Bitrix24 REST API client using webhook authentication.

    Authentication: URL-based webhook pattern:
        {base_url}/rest/{user_id}/{token}/{method}

    Example::

        client = BitrixClient(
            webhook_url="https://b24.example.com/rest/USER_ID/TOKEN",
        )
        tasks = client.get_tasks(responsible_id=70, start=0)
    """

    def __init__(
        self,
        webhook_url: str,
        timeout: int = 30,
        rate_limit_sleep: float = 2.0,
    ) -> None:
        """Initialize the Bitrix24 client.

        Args:
            webhook_url: Full webhook URL without trailing method
                (e.g. ``https://b24.example.com/rest/USER_ID/TOKEN``).
            timeout: Request timeout in seconds.
            rate_limit_sleep: Sleep time on 429 Too Many Requests.
        """
        self._base_url = webhook_url.rstrip("/")
        self._timeout = timeout
        self._rate_limit_sleep = rate_limit_sleep

        self._client = httpx.Client(
            headers={"Content-Type": "application/json"},
            timeout=httpx.Timeout(self._timeout),
        )

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    def __enter__(self) -> "BitrixClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # Map camelCase fields from tasks.task.list to SCREAMING_SNAKE
    # used by the poller (_process_task, _build_ticket_content).
    _FIELD_MAP: dict[str, str] = {
        "id": "ID",
        "title": "TITLE",
        "description": "DESCRIPTION",
        "status": "STATUS",
        "createdDate": "CREATED_DATE",
        "deadline": "DEADLINE",
        "responsibleId": "RESPONSIBLE_ID",
        "createdBy": "CREATED_BY",
    }

    def get_tasks(
        self,
        responsible_id: int,
        start: int = 0,
        order: str | None = None,
    ) -> dict[str, Any]:
        """Get list of tasks from Bitrix24 with pagination.

        Uses ``tasks.task.list.json`` endpoint with filter by
        responsible_id. Returns 50 tasks per page.

        Note: ``task.ctasks.getlist.json`` does NOT honour the
        ``filter[RESPONSIBLE_ID]`` parameter and returns tasks from
        arbitrary users. ``tasks.task.list.json`` filters correctly.

        Args:
            responsible_id: Bitrix24 user ID to filter tasks.
            start: Pagination offset (0, 50, 100, ...).
            order: Sort order (e.g. ``"CREATED_DATE"``).

        Returns:
            Dict with ``tasks`` list and ``next`` offset for pagination.
        """
        params: dict[str, Any] = {
            "filter[RESPONSIBLE_ID]": responsible_id,
            "start": start,
        }
        if order:
            params["order"] = order

        result = self._call("tasks.task.list.json", params=params)

        # tasks.task.list returns:
        # {"result": {"tasks": [...]}, "next": N, "total": N, "time": {...}}
        # NOTE: "next" and "total" are at TOP level, NOT inside "result"
        result_data = result.get("result", {})
        raw_tasks = result_data.get("tasks", [])
        next_offset = result.get("next", 0)

        # Map camelCase → SCREAMING_SNAKE for poller compatibility
        tasks = [self._map_task_fields(t) for t in raw_tasks]

        return {"tasks": tasks, "next": next_offset}

    def _map_task_fields(self, task: dict[str, Any]) -> dict[str, Any]:
        """Map camelCase field names from tasks.task.list to SCREAMING_SNAKE."""
        return {self._FIELD_MAP.get(k, k.upper()): v for k, v in task.items()}

    def get_task(self, task_id: int) -> dict[str, Any]:
        """Get a single task by ID.

        Uses ``task.get.json`` endpoint.

        Args:
            task_id: Bitrix24 task ID.

        Returns:
            Full task data dict with all fields.
        """
        result = self._call("task.get.json", params={"TASK_ID": task_id})

        # task.get returns {"result": {"task": {...}}}
        task_data = result.get("result", {})
        if isinstance(task_data, dict) and "task" in task_data:
            return task_data["task"]
        return task_data

    def delete_task(self, task_id: int) -> dict[str, Any]:
        """Delete a task in Bitrix24.

        Uses ``tasks.task.delete.json`` endpoint.

        Args:
            task_id: Bitrix24 task ID.

        Returns:
            Dict with ``task: true`` on success.

        Raises:
            RuntimeError: On HTTP failure.
        """
        result = self._call(
            "tasks.task.delete.json",
            params={"TASK_ID": task_id},
        )
        return result.get("result", {})

    def _call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Low-level request helper with retry on 5xx and rate-limit handling.

        Args:
            method: Bitrix24 REST method name (e.g. ``tasks.task.list.json``).
            params: Optional query parameters.

        Returns:
            Parsed JSON response.

        Raises:
            RuntimeError: On HTTP failure after retries.
        """
        url = f"{self._base_url}/{method}"
        retries = 1

        for attempt in range(retries + 1):
            try:
                response = self._client.get(url, params=params)
            except httpx.RequestError as exc:
                if attempt < retries:
                    logger.warning(
                        "Bitrix24 request failed (attempt %d/%d): %s",
                        attempt + 1,
                        retries + 1,
                        exc,
                    )
                    time.sleep(2)
                    continue
                raise RuntimeError(
                    f"Bitrix24 request failed: {method} — {exc}"
                ) from exc

            # Rate limit handling
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                sleep_time = (
                    float(retry_after) if retry_after else self._rate_limit_sleep
                )
                logger.warning(
                    "Bitrix24 rate limited (429), sleeping %.1fs", sleep_time
                )
                time.sleep(sleep_time)
                continue

            # Retry on 5xx
            if response.status_code >= 500 and attempt < retries:
                logger.warning(
                    "Bitrix24 server error %d (attempt %d/%d)",
                    response.status_code,
                    attempt + 1,
                    retries + 1,
                )
                time.sleep(2)
                continue

            if not response.is_success:
                raise RuntimeError(
                    f"Bitrix24 returned HTTP {response.status_code} for {method}"
                    + (f": {response.text[:200]}" if response.text else "")
                )

            try:
                return response.json()
            except Exception as exc:
                raise RuntimeError(
                    f"Bitrix24 returned non-JSON for {method}: {exc}"
                ) from exc

        # Should not reach here, but for type safety
        raise RuntimeError(f"Bitrix24 request failed after {retries + 1} attempts")
