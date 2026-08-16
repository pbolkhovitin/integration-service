"""Mirror Bitrix24 task chat comments into GLPI ticket followups.

Bidirectional chat mapping:
- GLPI → B24: followups are written to the Bitrix24 chat (reverse_sync).
- B24 → GLPI: this module mirrors task comments into GLPI followups.

Loop protection: mirrored followups are recorded in ``mirrored_followups``
and reverse_sync skips them (they must not be sent back to Bitrix24).
"""

import asyncio
import logging

from sqlalchemy import select

from app.core.database import async_session_factory
from app.models.mirrored_followup import MirroredFollowup
from app.models.task import Task
from app.services.bitrix import BitrixClient
from app.services.glpi import GLPIClient

logger = logging.getLogger(__name__)


def _extract_glpi_ticket_id(task: Task) -> int | None:
    """Extract the GLPI ticket ID from a Task record's result field."""
    if not task.result:
        return None
    if isinstance(task.result, list) and task.result:
        first = task.result[0]
        if isinstance(first, dict):
            return first.get("id")
    if isinstance(task.result, dict):
        return task.result.get("id") or task.result.get("tickets_id")
    return None


async def record_mirrored_followup(
    task_id: int, glpi_followup_id: int
) -> None:
    async with async_session_factory() as db:
        exists = await db.execute(
            select(MirroredFollowup.id).where(
                MirroredFollowup.glpi_followup_id == glpi_followup_id
            )
        )
        if exists.scalar_one_or_none() is None:
            db.add(
                MirroredFollowup(
                    task_id=task_id, glpi_followup_id=glpi_followup_id
                )
            )
            await db.commit()


async def is_mirrored_followup(glpi_followup_id: int) -> bool:
    """True if the GLPI followup was mirrored from a Bitrix24 comment."""
    async with async_session_factory() as db:
        result = await db.execute(
            select(MirroredFollowup.id).where(
                MirroredFollowup.glpi_followup_id == glpi_followup_id
            )
        )
        return result.scalar_one_or_none() is not None


async def mirror_task_comments(
    task: Task,
    bitrix_client: BitrixClient,
    glpi_client: GLPIClient,
    glpi_session: str,
) -> int:
    """Mirror new Bitrix24 task-chat messages into GLPI ticket followups.

    Uses the ``im``-scoped webhook (settings.im_webhook_url) to read the
    task chat. Returns the number of mirrored messages.
    """
    ticket_id = _extract_glpi_ticket_id(task)
    if ticket_id is None:
        return 0

    # Fast path: no chat in the task payload → nothing to mirror.
    if not (task.payload or {}).get("CHATID"):
        return 0

    from app.config.settings import settings

    chat_client = BitrixClient(
        webhook_url=settings.im_webhook_url
    ) if settings.im_webhook_url else bitrix_client
    task_id = int(task.source_id)
    messages = await asyncio.to_thread(
        chat_client.get_chat_messages, task_id
    )
    if not messages:
        return 0

    last = task.last_b24_comment_id or 0
    if task.last_b24_comment_id is None:
        # First run: baseline to the latest message without replaying the
        # full chat history into GLPI (avoids flooding followups).
        new_last = max(m["id"] for m in messages)
        async with async_session_factory() as db:
            result = await db.execute(
                select(Task).where(
                    Task.source == "bitrix24",
                    Task.source_id == str(task_id),
                )
            )
            db_task = result.scalar_one_or_none()
            if db_task is not None:
                db_task.last_b24_comment_id = new_last
                await db.commit()
        logger.info(
            "Mirror: task %s chat baseline set to message %s (no replay)",
            task_id, new_last,
        )
        return 0

    new = sorted(
        (m for m in messages if m["id"] > last), key=lambda m: m["id"]
    )
    mirrored = 0
    new_last = last
    for m in new:
        try:
            followup = await asyncio.to_thread(
                glpi_client.add_followup,
                ticket_id,
                m["text"],
                glpi_session,
            )
            fid = None
            if isinstance(followup, dict):
                fid = followup.get("id")
            elif isinstance(followup, list) and followup:
                fid = followup[0].get("id")
            if fid:
                await record_mirrored_followup(task_id, int(fid))
                mirrored += 1
                new_last = max(new_last, m["id"])
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Mirror: failed to add followup for task %s message %s: %s",
                task_id, m["id"], exc,
            )

    if mirrored:
        async with async_session_factory() as db:
            result = await db.execute(
                select(Task).where(
                    Task.source == "bitrix24",
                    Task.source_id == str(task_id),
                )
            )
            db_task = result.scalar_one_or_none()
            if db_task is not None:
                db_task.last_b24_comment_id = new_last
                await db.commit()
    return mirrored
