"""Reverse sync: GLPI status changes and followups → Bitrix24.

Runs ONLY for whitelisted test task IDs when TEST_MODE=True.
"""

import asyncio
import logging

from sqlalchemy import select

from app.config.settings import settings
from app.core.database import async_session_factory
from app.models.task import Task
from app.services.bitrix import BitrixClient
from app.services.glpi import GLPIClient

logger = logging.getLogger(__name__)

# GLPI status → Bitrix24 status mapping
_GLPI_TO_BITRIX_STATUS: dict[int, int] = {
    1: 1,  # new → open
    2: 2,  # in progress / assigned → pending
    3: 4,  # on hold → frozen
    4: 3,  # resolved → closed
    5: 5,  # solved → completed
    6: 6,  # cancelled → deferred
}


def _extract_glpi_ticket_id(task: Task) -> int | None:
    """Extract GLPI ticket ID from a Task record's result field.

    The result can be:
    - A list: [{"id": 123, "message": "..."}] (from create_ticket)
    - A dict: {"id": 123} or {"tickets_id": 123}
    - None or empty
    """
    if not task.result:
        return None

    if isinstance(task.result, list) and task.result:
        first = task.result[0]
        if isinstance(first, dict):
            return first.get("id")

    if isinstance(task.result, dict):
        return task.result.get("id") or task.result.get("tickets_id")

    return None


def _extract_glpi_status(ticket_info: dict) -> int | None:
    """Extract numeric status from a GLPI ticket info dict.

    Handles both flat ``{"status": N}`` and GLPI-wrapped
    ``{"data": {"status": N}}`` or ``{"data": [{"status": N}]}`` responses.
    """
    # Flat response
    if "status" in ticket_info:
        return int(ticket_info["status"])

    # Wrapped response
    data = ticket_info.get("data")
    if isinstance(data, dict) and "status" in data:
        return int(data["status"])
    if isinstance(data, list) and data and isinstance(data[0], dict):
        status = data[0].get("status")
        if status is not None:
            return int(status)

    return None


async def reverse_sync_test_tasks() -> dict:
    """Check GLPI tickets for test tasks and sync changes to Bitrix24.

    For each whitelisted test task ID:
    1. Looks up the local DB record (source='bitrix24')
    2. Extracts the linked GLPI ticket ID
    3. Reads current GLPI status and followups
    4. If GLPI status changed → updates Bitrix24 status
    5. If new followups exist → adds comments to Bitrix24
    6. Records the last-seen GLPI status and followup ID in the DB

    Returns:
        Summary dict with keys: ``checked``, ``status_updated``,
        ``comments_sent``, ``errors``, ``glpi_followups_read``.
    """
    if not settings.TEST_MODE:
        return {"error": "TEST_MODE is disabled"}

    test_ids = settings.test_task_ids
    if not test_ids:
        return {"error": "No test task IDs configured"}

    bitrix_client = BitrixClient(webhook_url=settings.BITRIX24_WEBHOOK_URL)
    glpi_client = GLPIClient(
        base_url=settings.GLPI_URL,
        app_token=settings.GLPI_APP_TOKEN.get_secret_value(),
        user_token=settings.GLPI_USER_TOKEN.get_secret_value(),
    )

    summary: dict = {
        "checked": 0,
        "status_updated": 0,
        "comments_sent": 0,
        "errors": [],
        "glpi_followups_read": 0,
    }

    try:
        for task_id in test_ids:
            try:
                await _sync_one_task(
                    bitrix_client=bitrix_client,
                    glpi_client=glpi_client,
                    task_id=task_id,
                    summary=summary,
                )
            except Exception as exc:
                logger.error(
                    "Reverse sync failed for task %s: %s", task_id, exc,
                )
                summary["errors"].append(str(exc))
                continue
    finally:
        bitrix_client.close()
        glpi_client.close()

    return summary


async def _sync_one_task(
    bitrix_client: BitrixClient,
    glpi_client: GLPIClient,
    task_id: int,
    summary: dict,
) -> None:
    """Process a single test task for reverse sync."""
    # a. Query DB for Task
    async with async_session_factory() as db:
        result = await db.execute(
            select(Task).where(
                Task.source == "bitrix24",
                Task.source_id == str(task_id),
            ),
        )
        task = result.scalar_one_or_none()

    if task is None:
        logger.warning(
            "Reverse sync: task %s not found in DB, skipping", task_id,
        )
        return

    # c. Extract GLPI ticket ID
    glpi_ticket_id = _extract_glpi_ticket_id(task)
    if glpi_ticket_id is None:
        logger.warning(
            "Reverse sync: task %s has no GLPI ticket ID in result, skipping",
            task_id,
        )
        return

    # e. Init GLPI session (sync call in thread)
    glpi_session = await asyncio.to_thread(glpi_client.init_session)
    try:
        # f. Get followups (sync call in thread)
        followups = await asyncio.to_thread(
            glpi_client.get_ticket_followups,
            glpi_ticket_id,
            glpi_session,
        )

        # g. Get current ticket info (sync call in thread)
        ticket_info = await asyncio.to_thread(
            glpi_client.show_ticket,
            glpi_ticket_id,
            glpi_session,
        )

        # h. Extract current GLPI status
        current_glpi_status = _extract_glpi_status(ticket_info)
        if current_glpi_status is None:
            logger.warning(
                "Reverse sync: no status found in GLPI ticket %s",
                glpi_ticket_id,
            )
            return

        summary["checked"] += 1
        summary["glpi_followups_read"] += len(followups)

    # --- STATUS SYNC ---
        last_glpi_status: int | None = None
        if task.last_glpi_status is not None:
            try:
                last_glpi_status = int(task.last_glpi_status)
            except (ValueError, TypeError):
                last_glpi_status = None

        if last_glpi_status is None or current_glpi_status != last_glpi_status:
            mapped_status = _GLPI_TO_BITRIX_STATUS.get(current_glpi_status, 1)
            await asyncio.to_thread(
                bitrix_client.update_task_status,
                task_id,
                mapped_status,
            )
            summary["status_updated"] += 1
            logger.info(
                "Reverse sync: updated Bitrix24 task %s status to %d "
                "(GLPI: %d → Bitrix: %d)",
                task_id,
                mapped_status,
                current_glpi_status,
                mapped_status,
            )

        # --- FOLLOWUP SYNC (description append for forumTopicId=None tasks) ---
        last_followup_id: int = task.last_glpi_followup_id or 0
        max_followup_id: int = last_followup_id
        new_followups = [
            f for f in followups if f.get("id", 0) > last_followup_id
        ]

        if new_followups:
            # Get current Bitrix24 task description
            b24_task = await asyncio.to_thread(
                bitrix_client.get_task, task_id,
            )
            current_desc = b24_task.get("DESCRIPTION") or ""

            # Append all new followups to description
            updated_desc = current_desc
            for fu in new_followups:
                fu_id = fu.get("id", 0)
                fu_date = fu.get("date", "unknown date")
                fu_content = fu.get("content", "")
                # Truncate very long content (Bitrix24 description has limits)
                if len(fu_content) > 2000:
                    fu_content = fu_content[:2000] + "..."
                separator = "\n\n" if updated_desc else ""
                updated_desc += f"{separator}[GLPI {fu_date}] {fu_content}"
                if fu_id > max_followup_id:
                    max_followup_id = fu_id

            # Cap total description length to stay within Bitrix24 TEXT limit (~65KB)
            MAX_DESC_LENGTH = 60000
            if len(updated_desc) > MAX_DESC_LENGTH:
                updated_desc = updated_desc[:MAX_DESC_LENGTH] + "\n\n... [truncated]"

            # Write back once (single API call for all followups)
            await asyncio.to_thread(
                bitrix_client.update_task_description,
                task_id,
                updated_desc,
            )
            summary["comments_sent"] += len(new_followups)
            logger.info(
                "Reverse sync: appended %d followups to Bitrix24 task %s description",
                len(new_followups),
                task_id,
            )

        # --- UPDATE DB ---
        async with async_session_factory() as db:
            result = await db.execute(
                select(Task).where(
                    Task.source == "bitrix24",
                    Task.source_id == str(task_id),
                ),
            )
            db_task = result.scalar_one_or_none()
            if db_task is not None:
                db_task.last_glpi_status = str(current_glpi_status)
                db_task.last_glpi_followup_id = (
                    max_followup_id if max_followup_id > 0 else None
                )
                await db.commit()
    finally:
        # Always release the GLPI session, even on early return/error.
        try:
            await asyncio.to_thread(glpi_client.kill_session, glpi_session)
        except Exception:
            logger.warning(
                "Reverse sync: failed to kill GLPI session for task %s",
                task_id,
                exc_info=True,
            )


def get_reverse_sync_status() -> dict:
    """Return configuration status of the reverse sync feature."""
    return {
        "test_mode": settings.TEST_MODE,
        "test_task_ids": settings.test_task_ids,
        "active": bool(settings.TEST_MODE and settings.test_task_ids),
    }
