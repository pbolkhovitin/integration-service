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
        created_after: str | None = None,
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
            created_after: ISO datetime; only tasks created after this
                are returned (e.g. last-N-days window in dev/test).

        Returns:
            Dict with ``tasks`` list and ``next`` offset for pagination.
        """
        params: dict[str, Any] = {
            "filter[RESPONSIBLE_ID]": responsible_id,
            "start": start,
        }
        if order:
            params["order"] = order
        if created_after:
            params["filter[CREATED_DATE]"] = f">{created_after}"

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

    def get_task_tags(self, task_id: int) -> list[str]:
        """Get tags for a specific task.

        Uses legacy ``task.item.gettags.json`` endpoint.
        Note: ``tasks.task.get`` does NOT return tags — they require
        a separate call via this legacy method.

        Args:
            task_id: Bitrix24 task ID.

        Returns:
            List of tag strings (e.g. ``["Указать категорию!", "Приоритет"]``).
        """
        result = self._call(
            "task.item.gettags.json",
            params={"TASK_ID": task_id},
        )
        tags = result.get("result", [])
        if isinstance(tags, list):
            return [str(t) for t in tags]
        return []

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

    def update_task_status(self, task_id: int, status: int) -> dict[str, Any]:
        """Update a Bitrix24 task status.

        Uses ``tasks.task.update.json`` endpoint with a JSON body.

        Args:
            task_id: Bitrix24 task ID.
            status: Target status value (e.g. ``5`` for completed).

        Returns:
            The updated task dict from ``result.get("task", {})``.

        Raises:
            RuntimeError: On HTTP failure.
        """
        result = self._call(
            "tasks.task.update.json",
            json_body={"id": task_id, "fields": {"STATUS": status}},
        )
        return result.get("result", {}).get("task", {})

    def add_comment(self, task_id: int, message: str) -> dict[str, Any]:
        """Add a comment to a Bitrix24 task.

        Uses ``tasks.task.comment.add.json`` endpoint with a JSON body.

        Args:
            task_id: Bitrix24 task ID.
            message: Comment text.

        Returns:
            The API result from ``result.get("result", {})``.

        Raises:
            RuntimeError: On HTTP failure.
        """
        result = self._call(
            "tasks.task.comment.add.json",
            json_body={"taskId": task_id, "fields": {"POST_MESSAGE": message}},
        )
        return result.get("result", {})

    def update_task_description(
        self, task_id: int, description: str
    ) -> dict[str, Any]:
        """Update a Bitrix24 task's description.

        Uses ``tasks.task.update.json`` endpoint. This is the reliable way to
        send messages to tasks without a forum topic (``forumTopicId=None``).
        ``tasks.task.comment.add`` silently fails for those tasks.

        Args:
            task_id: Bitrix24 task ID.
            description: New full description text.

        Returns:
            The updated task dict from ``result.get("task", {})``.

        Raises:
            RuntimeError: On HTTP failure.
        """
        result = self._call(
            "tasks.task.update.json",
            json_body={
                "id": task_id,
                "fields": {"DESCRIPTION": description},
            },
        )
        return result.get("result", {}).get("task", {})

    def get_chat_id(self, task_id: int) -> int | None:
        """Return the task's chat ID (chat.id) or None if no chat exists.

        The task chat lives in the IM module; the task only references it via
        ``chat.id``. Tasks never communicated on may have no chat at all.
        """
        result = self._call("tasks.task.get.json", params={"taskId": task_id})
        task_data = result.get("result", {})
        if isinstance(task_data, dict) and "task" in task_data:
            task_data = task_data["task"]
        chat_id = task_data.get("chatId")
        if chat_id is None:
            chat_id = task_data.get("CHAT_ID")
        return int(chat_id) if chat_id else None

    def get_chat_messages(self, task_id: int, limit: int = 50) -> list[dict[str, Any]]:
        """Get the task chat messages (requires an ``im``-scoped webhook).

        Uses ``im.v2.Chat.Message.list`` (modern portal) or falls back to the
        legacy ``task.commentitem.getlist`` when the task has no chat.

        Returns a list of messages with ``id``, ``text``, ``author_id``,
        ``date``. Empty when the task has no chat.
        """
        chat_id = self.get_chat_id(task_id)
        if not chat_id:
            return []
        try:
            result = self._call(
                "im.v2.Chat.Message.list",
                params={"chatId": chat_id, "limit": limit},
            )
        except RuntimeError as exc:
            logger.warning("im.v2.Chat.Message.list failed: %s", exc)
            return []
        res = result.get("result", {})
        messages = res.get("messages", []) if isinstance(res, dict) else []
        out: list[dict[str, Any]] = []
        for m in messages or []:
            if not isinstance(m, dict):
                continue
            out.append(
                {
                    "id": int(m.get("id") or 0),
                    "text": str(m.get("text") or m.get("message") or ""),
                    "author_id": int(
                        m.get("authorId") or m.get("author_id") or 0
                    ),
                    "date": str(m.get("date") or ""),
                }
            )
        return out

    def add_chat_message(self, task_id: int, message: str) -> int | None:
        """Add a message to the task chat (requires an ``im``-scoped webhook).

        Uses ``im.message.add``. Returns the new message ID, or None when the
        task has no chat.
        """
        chat_id = self.get_chat_id(task_id)
        if not chat_id:
            return None
        result = self._call(
            "im.message.add",
            json_body={"CHAT_ID": chat_id, "MESSAGE": message},
        )
        msg_id = result.get("result")
        return int(msg_id) if msg_id else None

    def add_elapsed(
        self, task_id: int, seconds: int, comment: str = ""
    ) -> dict[str, Any]:
        """Record elapsed time against a Bitrix24 task.

        Uses ``task.elapseditem.add.json``. Bitrix24 refuses to complete a
        task without recorded elapsed time — this is how work time is
        accounted back from GLPI.

        Args:
            task_id: Bitrix24 task ID.
            seconds: Elapsed seconds to add.
            comment: Optional comment for the time record.

        Returns:
            The API result dict.

        Raises:
            RuntimeError: On HTTP failure.
        """
        fields: dict[str, Any] = {"SECONDS": seconds}
        if comment:
            fields["COMMENT"] = comment
        result = self._call(
            "task.elapseditem.add.json",
            json_body={"taskId": task_id, "fields": fields},
        )
        return result.get("result", {})

    def get_departments(self) -> list[dict[str, Any]]:
        """Get the company department tree from Bitrix24.

        Uses ``department.get.json``. Each department has ``ID``, ``NAME``,
        ``PARENT`` (parent department ID or None for the root), ``UF_HEAD``.

        Requires a webhook token with the ``department`` scope (the task
        polling token does not have it).

        Returns:
            List of department dicts with ``ID``/``NAME``/``PARENT`` keys.

        Raises:
            RuntimeError: On HTTP failure or missing scope.
        """
        result = self._call("department.get.json")
        departments = result.get("result", [])
        normalized = []
        for d in departments or []:
            parent = d.get("PARENT")
            normalized.append(
                {
                    "id": int(d.get("ID")),
                    "name": str(d.get("NAME", "")),
                    "parent_id": int(parent) if parent else None,
                }
            )
        return normalized

    def get_users(self, start: int = 0) -> dict[str, Any]:
        """Get active users from Bitrix24 (paginated, 50 per page).

        Uses ``user.get.json``. Each user has ``ID``, ``NAME``, ``LAST_NAME``,
        ``SECOND_NAME``, ``EMAIL``, ``WORK_POSITION``, ``ACTIVE``,
        ``UF_DEPARTMENT`` (list of department IDs).

        Requires a webhook token with the ``user`` scope.

        Args:
            start: Pagination offset (0, 50, 100, ...).

        Returns:
            Dict with ``users`` (normalized list) and ``next`` offset.
        """
        result = self._call("user.get.json", params={"start": start})
        users = result.get("result", [])
        normalized = []
        for u in users or []:
            normalized.append(
                {
                    "id": int(u.get("ID")),
                    "name": str(u.get("NAME", "")),
                    "last_name": str(u.get("LAST_NAME", "")),
                    "email": str(u.get("EMAIL", "") or ""),
                    "work_position": str(u.get("WORK_POSITION", "") or ""),
                    "phone": str(u.get("PERSONAL_PHONE", "") or ""),
                    "mobile": str(u.get("PERSONAL_MOBILE", "") or ""),
                    "active": bool(u.get("ACTIVE", False)),
                    "department_ids": [
                        int(x) for x in (u.get("UF_DEPARTMENT") or [])
                    ],
                }
            )
        return {"users": normalized, "next": result.get("next", 0)}

    def _call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        """Low-level request helper with retry on 5xx and rate-limit handling.

        Args:
            method: Bitrix24 REST method name (e.g. ``tasks.task.list.json``).
            params: Optional query parameters.
            json_body: Optional JSON body for POST requests.

        Returns:
            Parsed JSON response.

        Raises:
            RuntimeError: On HTTP failure after retries.
        """
        url = f"{self._base_url}/{method}"
        retries = 1

        for attempt in range(retries + 1):
            try:
                if json_body is not None:
                    response = self._client.post(url, json=json_body)
                else:
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
