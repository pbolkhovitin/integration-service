"""Reverse sync: GLPI status changes and followups → Bitrix24.

Runs ONLY for whitelisted test task IDs when TEST_MODE=True.
"""

import asyncio
import hashlib
import logging

from sqlalchemy import select

from app.config.settings import settings
from app.core.database import async_session_factory
from app.models.task import Task
from app.services.bitrix import BitrixClient
from app.services.comment_mirror import is_mirrored_followup
from app.services.glpi import GLPIClient
from app.services.test_tasks import allowed_test_task_ids

logger = logging.getLogger(__name__)

# GLPI status → Bitrix24 status mapping (reverse sync / L1 write-back).
_GLPI_TO_BITRIX_STATUS: dict[int, int] = {
    1: 1,  # new → new
    2: 3,  # assigned → in progress
    3: 3,  # planned → in progress
    4: 2,  # waiting → pending
    5: 5,  # solved → completed
    6: 5,  # closed → completed
}

_GLPI_PRIORITY_LABELS: dict[int, str] = {
    1: "Низкий",
    2: "Ниже среднего",
    3: "Средний",
    4: "Высокий",
    5: "Очень высокий",
}


def _build_l1_template_from_ticket(
    glpi_client: GLPIClient, ticket_info: dict, glpi_session: str
) -> str:
    """Compose the L1 description template from a GLPI ticket.

    Reads the ticket's requester (name/phone), category and priority and
    renders the standardized L1 template written back to Bitrix24.
    """
    from app.services.ticket_mapper import build_l1_template, extract_problem_description

    requester_id = None
    try:
        requesters = glpi_client.get_ticket_requesters(
            int(ticket_info.get("id") or 0), glpi_session
        )
        if requesters:
            requester_id = requesters[0]
    except Exception as exc:  # noqa: BLE001
        logger.warning("L1: failed to load requesters: %s", exc)
    if requester_id is None:
        raw = ticket_info.get("users_id_recipient")
        requester_id = int(raw) if raw else None

    fio = ""
    phone = ""
    if requester_id:
        try:
            user = glpi_client.get_user(requester_id, glpi_session)
            if isinstance(user, dict):
                fio = " ".join(
                    p for p in (user.get("realname") or "", user.get("firstname") or "") if p
                )
                phone = str(user.get("phone") or user.get("mobile") or "")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "L1: failed to load requester %s: %s", requester_id, exc
            )

    category = ""
    cat_id = ticket_info.get("itilcategories_id")
    if cat_id:
        try:
            cat = glpi_client.get_itilcategory(int(cat_id), glpi_session)
            if isinstance(cat, dict):
                category = str(cat.get("name") or "")
        except Exception as exc:  # noqa: BLE001
            logger.warning("L1: failed to load category %s: %s", cat_id, exc)

    priority = _GLPI_PRIORITY_LABELS.get(int(ticket_info.get("priority") or 0), "")

    return build_l1_template(
        fio=fio,
        phone=phone,
        organization="",
        location="",
        category=category,
        priority=priority,
        problem_description=extract_problem_description(
            ticket_info.get("content")
        ),
    )


def _is_whitelisted_task(task_id: int) -> bool:
    """Return True only for tasks explicitly listed in TEST_TASK_IDS.

    This is the single write-guard for Bitrix24: reverse sync must never
    write to a task that is not on the test whitelist, regardless of how
    it was invoked (scheduled job, manual endpoint, direct call).
    """
    return task_id in settings.test_task_ids


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

    # Process the FULL writable whitelist (env TEST_TASK_IDS + runtime
    # bitrix_test_tasks additions, e.g. auto-whitelisted "Test_GLPI" tasks).
    test_ids = sorted(await allowed_test_task_ids())
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
        "skipped_not_whitelisted": 0,
        "l1_updated": 0,
        "elapsed_added": 0,
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
    # Whitelist guard: never write to Bitrix24 for a non-test task.
    # Checks env TEST_TASK_IDS + runtime bitrix_test_tasks table.
    if task_id not in await allowed_test_task_ids():
        logger.warning(
            "Reverse sync: task %s is NOT in TEST_TASK_IDS whitelist — "
            "refusing to write to Bitrix24",
            task_id,
        )
        summary["skipped_not_whitelisted"] += 1
        return

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

    # --- L1 TEMPLATE (uniform DESCRIPTION in Bitrix24) ---
        l1_template = _build_l1_template_from_ticket(
            glpi_client, ticket_info, glpi_session
        )
        l1_hash = hashlib.sha256(l1_template.encode()).hexdigest()
        if task.last_l1_hash != l1_hash:
            await asyncio.to_thread(
                bitrix_client.update_task_description,
                task_id,
                l1_template,
            )
            summary["l1_updated"] += 1
            logger.info(
                "Reverse sync: wrote L1 template to Bitrix24 task %s", task_id
            )
            last_l1_hash = l1_hash
        else:
            last_l1_hash = task.last_l1_hash

    # --- TIME SYNC (elapseditem.add, min fallback) — BEFORE status, so a
    # completed status is written only after the task has elapsed time
    # (Bitrix24 refuses to complete a task without time). ---
        last_elapsed: int = task.last_elapsed_synced or 0
        actiontime = await asyncio.to_thread(
            glpi_client.sum_ticket_actiontime, glpi_ticket_id, glpi_session
        )
        if actiontime > last_elapsed:
            minutes = max(actiontime, settings.L1_MIN_ELAPSED_SECONDS)
            await asyncio.to_thread(
                bitrix_client.add_elapsed, task_id, minutes
            )
            summary["elapsed_added"] += 1
            logger.info(
                "Reverse sync: added %ds elapsed to Bitrix24 task %s",
                minutes, task_id,
            )
            last_elapsed_synced = actiontime
        else:
            last_elapsed_synced = last_elapsed

    # --- STATUS SYNC (must not abort the whole task on error) ---
        last_glpi_status: int | None = None
        if task.last_glpi_status is not None:
            try:
                last_glpi_status = int(task.last_glpi_status)
            except (ValueError, TypeError):
                last_glpi_status = None

        if last_glpi_status is None or current_glpi_status != last_glpi_status:
            mapped_status = _GLPI_TO_BITRIX_STATUS.get(current_glpi_status, 1)
            try:
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
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Reverse sync: status update failed for task %s "
                    "(GLPI: %d → Bitrix: %d): %s",
                    task_id, current_glpi_status, mapped_status, exc,
                )

    # --- FOLLOWUP SYNC (task chat; no chat → description) ---
        last_followup_id: int = task.last_glpi_followup_id or 0
        max_followup_id: int = last_followup_id
        new_followups = [
            f for f in followups if f.get("id", 0) > last_followup_id
        ]

        # Tasks without a chat (no chatId) have nowhere to send messages —
        # route comments to the DESCRIPTION (visible).
        fallback_contents: list[str] = []
        chat_client = bitrix_client
        if settings.im_webhook_url:
            chat_client = BitrixClient(webhook_url=settings.im_webhook_url)

        for fu in new_followups:
            fu_id = fu.get("id", 0)
            # Skip followups mirrored FROM Bitrix24 chat (loop protection).
            if fu_id and await is_mirrored_followup(fu_id):
                logger.debug(
                    "Reverse sync: skip mirrored followup %s (from B24)", fu_id
                )
                max_followup_id = max(max_followup_id, fu_id)
                continue
            fu_content = fu.get("content", "")
            if len(fu_content) > 2000:
                fu_content = fu_content[:2000] + "..."
            msg_id = await asyncio.to_thread(
                chat_client.add_chat_message, task_id, fu_content
            )
            if msg_id is not None:
                summary["comments_sent"] += 1
            else:
                logger.info(
                    "Task %s has no chat — comment routed to description", task_id
                )
                fallback_contents.append(fu_content)
            max_followup_id = max(max_followup_id, fu_id)

        if fallback_contents:
            b24_task = await asyncio.to_thread(bitrix_client.get_task, task_id)
            current_desc = b24_task.get("DESCRIPTION") or ""
            for content in fallback_contents:
                separator = "\n\n" if current_desc else ""
                current_desc += f"{separator}[GLPI followup] {content}"
                summary["comments_sent"] += 1
            if len(current_desc) > 60000:
                current_desc = current_desc[:60000] + "\n\n... [truncated]"
            await asyncio.to_thread(
                bitrix_client.update_task_description,
                task_id,
                current_desc,
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
                db_task.last_l1_hash = last_l1_hash
                db_task.last_elapsed_synced = last_elapsed_synced
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
        "auto_write_enabled": settings.BITRIX24_REVERSE_SYNC_ENABLED,
    }
