"""APScheduler-based poller for Bitrix24 tasks.

Periodically polls Bitrix24 REST API for new/updated tasks and creates
corresponding GLPI tickets. Runs inside the FastAPI process (no Celery needed).

Also performs reconciliation: detects tasks deleted in Bitrix24 and
closes corresponding GLPI tickets (status=5 solved).
"""

import asyncio
import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select, func as sa_func

from app.config.settings import settings
from app.core.database import async_session_factory
from app.models.task import Task
from app.services.bitrix import BitrixClient
from app.services.glpi import GLPIClient

logger = logging.getLogger(__name__)

# Global scheduler instance
_scheduler: AsyncIOScheduler | None = None

# Safety: skip orphan detection if fewer than this many tasks were fetched
# (could indicate API error or network issue)
_MIN_TASKS_FOR_RECONCILIATION = 10


async def _poll_bitrix24() -> None:
    """Poll Bitrix24 for tasks and create GLPI tickets.

    This is the core polling job that runs on schedule. It:
    1. Fetches tasks from Bitrix24 for each responsible_id
    2. Checks idempotency (skip already processed tasks)
    3. Creates GLPI tickets for new tasks
    4. Reconciles: detects deleted tasks and closes GLPI tickets
    5. Records results in the database
    """
    if not settings.BITRIX24_WEBHOOK_URL:
        logger.debug("Skipping poll: BITRIX24_WEBHOOK_URL not configured")
        return

    responsible_ids = settings.responsible_ids
    if not responsible_ids:
        logger.debug("Skipping poll: BITRIX24_RESPONSIBLE_IDS not configured")
        return

    logger.info(
        "Polling Bitrix24 for %d responsible IDs: %s",
        len(responsible_ids),
        responsible_ids,
    )

    bitrix_client = BitrixClient(webhook_url=settings.BITRIX24_WEBHOOK_URL)
    glpi_client = GLPIClient(
        base_url=settings.GLPI_URL,
        app_token=settings.GLPI_APP_TOKEN.get_secret_value(),
        user_token=settings.GLPI_USER_TOKEN.get_secret_value(),
    )

    # Collect all Bitrix24 task IDs fetched this cycle
    all_fetched_ids: set[str] = set()

    try:
        # Init GLPI session
        glpi_session = glpi_client.init_session()
        logger.debug("GLPI session initialized: %s...", glpi_session[:8])

        for responsible_id in responsible_ids:
            fetched = await _poll_for_user(
                bitrix_client=bitrix_client,
                glpi_client=glpi_client,
                glpi_session=glpi_session,
                responsible_id=responsible_id,
            )
            all_fetched_ids.update(fetched)

        # Reconciliation: detect deleted tasks and close GLPI tickets
        await _reconcile_deletions(
            bitrix_client=bitrix_client,
            glpi_client=glpi_client,
            glpi_session=glpi_session,
            all_fetched_ids=all_fetched_ids,
        )
    except Exception:
        logger.exception("Poll cycle failed")
    finally:
        bitrix_client.close()
        glpi_client.close()


async def _poll_for_user(
    bitrix_client: BitrixClient,
    glpi_client: GLPIClient,
    glpi_session: str,
    responsible_id: int,
) -> set[str]:
    """Poll Bitrix24 for a single user's tasks.

    Returns:
        Set of all Bitrix24 task IDs fetched for this user (used by
        reconciliation to detect deletions).
    """
    start = 0
    total_processed = 0
    total_skipped = 0
    fetched_ids: set[str] = set()

    while True:
        # Fetch page of tasks from Bitrix24
        try:
            page = bitrix_client.get_tasks(
                responsible_id=responsible_id,
                start=start,
            )
        except RuntimeError as exc:
            logger.error(
                "Failed to fetch Bitrix24 tasks for user %d: %s",
                responsible_id,
                exc,
            )
            break

        tasks = page.get("tasks", [])
        next_offset = page.get("next", 0)

        if not tasks:
            break

        # Process each task
        for task_data in tasks:
            task_id = str(task_data.get("ID", ""))
            if task_id:
                fetched_ids.add(task_id)

            result = await _process_task(
                bitrix_client=bitrix_client,
                glpi_client=glpi_client,
                glpi_session=glpi_session,
                task_data=task_data,
            )
            if result == "created":
                total_processed += 1
            elif result == "skipped":
                total_skipped += 1

        # Check if there are more pages
        if next_offset == 0 or next_offset <= start:
            break
        start = next_offset

    logger.info(
        "User %d: fetched %d tasks, processed %d, skipped %d",
        responsible_id,
        len(fetched_ids),
        total_processed,
        total_skipped,
    )

    return fetched_ids


async def _process_task(
    bitrix_client: BitrixClient,
    glpi_client: GLPIClient,
    glpi_session: str,
    task_data: dict,
) -> str:
    """Process a single Bitrix24 task.

    Returns:
        "created" if a GLPI ticket was created,
        "skipped" if the task was already processed or should be skipped.
    """
    task_id = str(task_data.get("ID", ""))
    title = task_data.get("TITLE", "Untitled")
    description = task_data.get("DESCRIPTION", "")
    status = task_data.get("STATUS")

    if not task_id:
        logger.warning("Skipping task without ID: %s", task_data)
        return "skipped"

    # Skip closed/completed tasks (status 3=closed, 5=closed+completed)
    if status in (3, 5):
        logger.debug("Skipping completed task %s", task_id)
        return "skipped"

    # Idempotency check: have we already processed this task?
    async with async_session_factory() as db:
        result = await db.execute(
            select(Task).where(
                Task.source == "bitrix24",
                Task.source_id == task_id,
            )
        )
        existing = result.scalar_one_or_none()
        if existing is not None:
            if existing.status == "completed":
                return "skipped"
            # If failed, we could retry, but for MVP skip
            return "skipped"

    # Build GLPI ticket content
    content = _build_ticket_content(task_data)

    # Create Task record first (for idempotency)
    async with async_session_factory() as db:
        task = Task(
            source="bitrix24",
            source_id=task_id,
            type="create_ticket",
            payload=task_data,
            status="processing",
            idempotency_key=f"b24:{task_id}",
        )
        db.add(task)
        await db.commit()

    # Create GLPI ticket via sync call in thread
    try:
        ticket = await asyncio.to_thread(
            glpi_client.create_ticket,
            name=f"[Bitrix24 #{task_id}] {title}",
            content=content,
            session_token=glpi_session,
        )
    except Exception as exc:
        logger.error("Failed to create GLPI ticket for task %s: %s", task_id, exc)
        async with async_session_factory() as db:
            result = await db.execute(
                select(Task).where(
                    Task.source == "bitrix24",
                    Task.source_id == task_id,
                )
            )
            task = result.scalar_one_or_none()
            task.status = "failed"
            task.last_error = str(exc)
            await db.commit()
        return "skipped"

    # Mark task as completed
    async with async_session_factory() as db:
        result = await db.execute(
            select(Task).where(
                Task.source == "bitrix24",
                Task.source_id == task_id,
            )
        )
        task = result.scalar_one_or_none()
        if task:
            task.status = "completed"
            task.result = ticket
            await db.commit()

    logger.info(
        "Created GLPI ticket %s for Bitrix24 task %s: %s",
        ticket,
        task_id,
        title,
    )
    return "created"


def _build_ticket_content(task_data: dict) -> str:
    """Build GLPI ticket description from Bitrix24 task data."""
    lines = [
        f"[Bitrix24 Task #{task_data.get('ID', '?')}]",
        f"Title: {task_data.get('TITLE', 'N/A')}",
        f"Created: {task_data.get('CREATED_DATE', 'N/A')}",
        f"Deadline: {task_data.get('DEADLINE', 'N/A')}",
        f"Responsible ID: {task_data.get('RESPONSIBLE_ID', 'N/A')}",
        f"Created by ID: {task_data.get('CREATED_BY', 'N/A')}",
        "",
        "Description:",
        task_data.get("DESCRIPTION", "N/A"),
    ]
    return "\n".join(lines)


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


async def _reconcile_deletions(
    bitrix_client: BitrixClient,
    glpi_client: GLPIClient,
    glpi_session: str,
    all_fetched_ids: set[str],
) -> None:
    """Detect tasks deleted in Bitrix24 and close corresponding GLPI tickets.

    Compares all task IDs currently in our DB (source='bitrix24') against
    the set of IDs fetched from Bitrix24 this cycle. Tasks present in DB
    but absent from Bitrix24 are assumed deleted — their GLPI tickets are
    updated to status=5 (solved).

    Safety: skips reconciliation if fewer than _MIN_TASKS_FOR_RECONCILIATION
    tasks were fetched (could indicate API error).
    """
    if len(all_fetched_ids) < _MIN_TASKS_FOR_RECONCILIATION:
        logger.warning(
            "Skipping reconciliation: only %d tasks fetched (minimum %d). "
            "Possible API error — will retry next cycle.",
            len(all_fetched_ids),
            _MIN_TASKS_FOR_RECONCILIATION,
        )
        return

    # Get all completed bitrix24 tasks from our DB
    async with async_session_factory() as db:
        result = await db.execute(
            select(Task).where(
                Task.source == "bitrix24",
                Task.status == "completed",
            )
        )
        db_tasks = result.scalars().all()

    if not db_tasks:
        logger.debug("Reconciliation: no tasks in DB to check")
        return

    # Find orphaned tasks: in DB but not in Bitrix24 response
    orphans = []
    for task in db_tasks:
        if task.source_id not in all_fetched_ids:
            orphans.append(task)

    if not orphans:
        logger.debug(
            "Reconciliation: all %d synced tasks still present in Bitrix24",
            len(db_tasks),
        )
        return

    logger.info(
        "Reconciliation: found %d orphaned tasks (deleted from Bitrix24)",
        len(orphans),
    )

    closed_count = 0
    for task in orphans:
        # Extract GLPI ticket ID from task.result
        glpi_ticket_id = _extract_glpi_ticket_id(task)

        if not glpi_ticket_id:
            logger.warning(
                "Orphan task %s (Bitrix24 #%s): no GLPI ticket ID in result, "
                "skipping GLPI update",
                task.id,
                task.source_id,
            )
            continue

        try:
            await asyncio.to_thread(
                glpi_client.update_ticket,
                ticket_id=int(glpi_ticket_id),
                session_token=glpi_session,
                status=5,  # 5 = solved
            )
            logger.info(
                "Closed GLPI ticket %s for deleted Bitrix24 task %s",
                glpi_ticket_id,
                task.source_id,
            )
            closed_count += 1
        except Exception as exc:
            logger.error(
                "Failed to close GLPI ticket %s for task %s: %s",
                glpi_ticket_id,
                task.source_id,
                exc,
            )

    logger.info(
        "Reconciliation complete: %d/%d orphaned tasks closed in GLPI",
        closed_count,
        len(orphans),
    )


# ------------------------------------------------------------------
# Manual cleanup endpoint
# ------------------------------------------------------------------


async def cleanup_orphaned_tasks() -> dict:
    """Manually trigger orphan detection and GLPI ticket closure.

    Returns a summary of what was found and closed.
    """
    if not settings.BITRIX24_WEBHOOK_URL:
        return {"error": "BITRIX24_WEBHOOK_URL not configured"}

    bitrix_client = BitrixClient(webhook_url=settings.BITRIX24_WEBHOOK_URL)
    glpi_client = GLPIClient(
        base_url=settings.GLPI_URL,
        app_token=settings.GLPI_APP_TOKEN.get_secret_value(),
        user_token=settings.GLPI_USER_TOKEN.get_secret_value(),
    )

    all_fetched_ids: set[str] = set()

    try:
        glpi_session = glpi_client.init_session()

        # Fetch all tasks from Bitrix24
        for responsible_id in settings.responsible_ids:
            start = 0
            while True:
                page = bitrix_client.get_tasks(
                    responsible_id=responsible_id,
                    start=start,
                )
                tasks = page.get("tasks", [])
                next_offset = page.get("next", 0)

                for t in tasks:
                    tid = str(t.get("ID", ""))
                    if tid:
                        all_fetched_ids.add(tid)

                if next_offset == 0 or next_offset <= start:
                    break
                start = next_offset

        # Get DB tasks
        async with async_session_factory() as db:
            result = await db.execute(
                select(Task).where(
                    Task.source == "bitrix24",
                    Task.status == "completed",
                )
            )
            db_tasks = result.scalars().all()

        orphans = [t for t in db_tasks if t.source_id not in all_fetched_ids]

        closed = 0
        for task in orphans:
            glpi_id = _extract_glpi_ticket_id(task)

            if glpi_id:
                try:
                    await asyncio.to_thread(
                        glpi_client.update_ticket,
                        ticket_id=int(glpi_id),
                        session_token=glpi_session,
                        status=5,
                    )
                    closed += 1
                except Exception as exc:
                    logger.error("Failed to close ticket %s: %s", glpi_id, exc)

        return {
            "bitrix24_tasks_fetched": len(all_fetched_ids),
            "db_tasks_total": len(db_tasks),
            "orphans_found": len(orphans),
            "glpi_tickets_closed": closed,
            "orphan_ids": [t.source_id for t in orphans],
        }

    finally:
        bitrix_client.close()
        glpi_client.close()


def start_poller() -> None:
    """Start the APScheduler-based poller."""
    global _scheduler

    if not settings.BITRIX24_WEBHOOK_URL:
        logger.info("Poller not started: BITRIX24_WEBHOOK_URL not configured")
        return

    if not settings.responsible_ids:
        logger.info("Poller not started: BITRIX24_RESPONSIBLE_IDS not configured")
        return

    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(
        _poll_bitrix24,
        "interval",
        seconds=settings.BITRIX24_POLL_INTERVAL_SECONDS,
        id="bitrix24_poll",
        name="Bitrix24 Task Poller",
        max_instances=1,
        next_run_time=datetime.now(timezone.utc),  # Run immediately on startup
    )
    _scheduler.start()
    logger.info(
        "Poller started: interval=%ds, responsible_ids=%s",
        settings.BITRIX24_POLL_INTERVAL_SECONDS,
        settings.responsible_ids,
    )


def stop_poller() -> None:
    """Stop the APScheduler-based poller."""
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Poller stopped")


def get_poller_status() -> dict:
    """Get current poller status."""
    global _scheduler
    if _scheduler is None:
        return {"status": "not_started"}

    jobs = _scheduler.get_jobs()
    job = jobs[0] if jobs else None

    return {
        "status": "running",
        "interval_seconds": settings.BITRIX24_POLL_INTERVAL_SECONDS,
        "responsible_ids": settings.responsible_ids,
        "next_run": job.next_run_time.isoformat() if job else None,
    }
