"""APScheduler-based poller for Bitrix24 tasks.

Periodically polls Bitrix24 REST API for new/updated tasks and creates
corresponding GLPI tickets. Runs inside the FastAPI process (no Celery needed).

Also performs reconciliation: detects tasks deleted in Bitrix24 and
closes corresponding GLPI tickets (status=5 solved).
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config.settings import settings
from app.core.database import async_session_factory
from app.models.task import Task
from app.services.bitrix import BitrixClient
from app.services.glpi import GLPIClient
from app.services.org_sync import sync_org_structure
from app.services.reverse_sync import reverse_sync_test_tasks

logger = logging.getLogger(__name__)

# Global scheduler instance
_scheduler: AsyncIOScheduler | None = None

# Safety: skip orphan detection if fewer than this many tasks were fetched
# (could indicate API error or network issue)
_MIN_TASKS_FOR_RECONCILIATION = 10

# Closed/inactive Bitrix24 statuses that are not processed:
# 4 — awaiting control (supposedly completed), 5 — completed, 6/7 — deferred.
# 1 (new), 2 (pending), 3 (in progress) are still active and processed.
_SKIPPED_BITRIX_STATUSES = {4, 5, 6, 7}


def _is_skipped_bitrix_status(status) -> bool:
    """Return True if a Bitrix24 task status means the task is closed/inactive."""
    return status in _SKIPPED_BITRIX_STATUSES


def _stale_processing_seconds() -> int:
    """Threshold after which a ``processing`` task is considered stuck.

    Must exceed the time needed to process one page (GLPI timeout is 30s),
    but not be smaller than two poll intervals.
    """
    return max(2 * settings.BITRIX24_POLL_INTERVAL_SECONDS, 60)


def _should_retry_task(task: Task, now: datetime | None = None) -> bool:
    """Decide whether an existing Task should be processed again.

    ``completed``/``cancelled`` are never retried. ``failed`` tasks are
    retried while attempts remain. ``processing`` tasks are retried only
    when stale (stuck after a crash) — otherwise another worker may still
    be handling them.
    """
    if task.status in ("completed", "cancelled"):
        return False
    if task.status == "failed":
        return task.attempts < task.max_attempts
    if task.status == "processing":
        now = now or datetime.now(timezone.utc)
        updated = task.updated_at
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        return (now - updated).total_seconds() > _stale_processing_seconds()
    return True  # pending and any other statuses


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

    glpi_session: str | None = None

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
        if glpi_session is not None:
            try:
                glpi_client.kill_session(glpi_session)
            except Exception:
                logger.warning("Failed to kill GLPI session", exc_info=True)
        bitrix_client.close()
        glpi_client.close()


def _parse_task_date(value) -> datetime | None:
    """Parse a Bitrix24 datetime (e.g. 2026-08-14T10:00:00+03:00) to UTC."""
    if not value:
        return None
    try:
        dt = value
        if isinstance(value, str):
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


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

    # Dev/test window: only tasks created within the lookback window are
    # PROCESSED into tickets (BITRIX24_SYNC_LOOKBACK_DAYS=0 → all). All
    # fetched IDs still participate in reconciliation, so older tasks are
    # not mistaken for deletions. Bitrix24 list filters do not support
    # date ranges on this portal — filtering is done client-side.
    lookback = settings.BITRIX24_SYNC_LOOKBACK_DAYS
    since_dt: datetime | None = None
    if lookback > 0:
        since_dt = datetime.now(timezone.utc) - timedelta(days=lookback)

    while True:
        # Fetch page of tasks from Bitrix24 (blocking sync call in thread)
        try:
            page = await asyncio.to_thread(
                bitrix_client.get_tasks,
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

            # Client-side lookback window (Bitrix24 filters don't support
            # date ranges here): skip processing old tasks, but keep them
            # in fetched_ids so reconciliation does not close their tickets.
            if since_dt is not None:
                created = _parse_task_date(task_data.get("CREATED_DATE"))
                if created is None or created < since_dt:
                    continue

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

    # Skip closed/inactive tasks (4=awaiting control, 5=completed, 6/7=deferred)
    # unless INCLUDE_CLOSED_TASKS is enabled (dev/full-sync mode).
    if _is_skipped_bitrix_status(status) and not settings.INCLUDE_CLOSED_TASKS:
        logger.debug("Skipping inactive task %s (status=%s)", task_id, status)
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
            if not _should_retry_task(existing):
                return "skipped"
            # Retry failed/stale task: bump attempts and reset state.
            existing.attempts += 1
            existing.status = "processing"
            existing.last_error = None
            await db.commit()

    # Build GLPI ticket content
    # Fetch tags only for new tasks (not in DB yet) to avoid excess API calls
    try:
        tags = await asyncio.to_thread(
            bitrix_client.get_task_tags, int(task_id)
        )
        task_data["TAGS"] = tags
    except Exception as exc:
        logger.warning("Failed to fetch tags for task %s: %s", task_id, exc)
        task_data["TAGS"] = []

    content = _build_ticket_content(task_data)

    # Create Task record first (for idempotency)
    try:
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
    except IntegrityError:
        # A concurrent poll cycle created the same task first — skip it.
        logger.info("Task %s already exists (concurrent poll) — skipping", task_id)
        return "skipped"

    # Create GLPI ticket via sync call in thread
    try:
        ticket = await asyncio.to_thread(
            glpi_client.create_ticket,
            name=f"[Bitrix24 #{task_id}] {title}",
            content=content,
            session_token=glpi_session,
            category_id=settings.GLPI_DEFAULT_CATEGORY_ID,
            group_id=settings.GLPI_DEFAULT_GROUP_ID,
            entity_id=settings.GLPI_DEFAULT_ENTITY_ID,
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
            task.attempts += 1
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
    ]

    # Include tags if present
    tags = task_data.get("TAGS", [])
    if tags:
        lines.append(f"Tags: {', '.join(tags)}")

    lines.extend([
        "",
        "Description:",
        task_data.get("DESCRIPTION", "N/A"),
    ])
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
    orphans = [t for t in db_tasks if t.source_id not in all_fetched_ids]

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

    closed_count = await _close_orphan_tickets(
        glpi_client=glpi_client,
        glpi_session=glpi_session,
        orphans=orphans,
    )

    logger.info(
        "Reconciliation complete: %d/%d orphaned tasks closed in GLPI",
        closed_count,
        len(orphans),
    )


async def _close_orphan_tickets(
    glpi_client: GLPIClient,
    glpi_session: str,
    orphans: list[Task],
) -> int:
    """Close GLPI tickets (status=5 solved) for orphaned tasks.

    Returns the number of tickets successfully closed.
    """
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

    return closed_count


async def _fetch_all_bitrix_task_ids(bitrix_client: BitrixClient) -> set[str]:
    """Fetch all Bitrix24 task IDs for all responsible users.

    Returns the full set of task IDs currently present in Bitrix24.
    """
    all_fetched_ids: set[str] = set()

    for responsible_id in settings.responsible_ids:
        start = 0
        while True:
            page = await asyncio.to_thread(
                bitrix_client.get_tasks,
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

    return all_fetched_ids


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

    glpi_session: str | None = None

    try:
        glpi_session = glpi_client.init_session()

        # Fetch all tasks from Bitrix24
        all_fetched_ids = await _fetch_all_bitrix_task_ids(bitrix_client)

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

        closed = await _close_orphan_tickets(
            glpi_client=glpi_client,
            glpi_session=glpi_session,
            orphans=orphans,
        )

        return {
            "bitrix24_tasks_fetched": len(all_fetched_ids),
            "db_tasks_total": len(db_tasks),
            "orphans_found": len(orphans),
            "glpi_tickets_closed": closed,
            "orphan_ids": [t.source_id for t in orphans],
        }

    finally:
        if glpi_session is not None:
            try:
                glpi_client.kill_session(glpi_session)
            except Exception:
                logger.warning("Failed to kill GLPI session", exc_info=True)
        bitrix_client.close()
        glpi_client.close()


async def retry_failed_tasks() -> dict:
    """Requeue failed bitrix24 tasks (attempts < max_attempts) to pending.

    Returns a summary of what was requeued.
    """
    async with async_session_factory() as db:
        result = await db.execute(
            select(Task).where(
                Task.source == "bitrix24",
                Task.status == "failed",
            )
        )
        tasks = result.scalars().all()
        requeued = 0
        for t in tasks:
            if t.attempts < t.max_attempts:
                t.status = "pending"
                t.last_error = None
                requeued += 1
        await db.commit()
        return {"requeued": requeued, "failed_total": len(tasks)}


def _register_poller_jobs(scheduler: AsyncIOScheduler) -> None:
    """Register the polling and (optionally) reverse-sync jobs."""
    scheduler.add_job(
        _poll_bitrix24,
        "interval",
        seconds=settings.BITRIX24_POLL_INTERVAL_SECONDS,
        id="bitrix24_poll",
        name="Bitrix24 Task Poller",
        max_instances=1,
        next_run_time=datetime.now(timezone.utc),  # Run immediately on startup
    )
    if (
        settings.BITRIX24_REVERSE_SYNC_ENABLED
        and settings.TEST_MODE
        and settings.test_task_ids
    ):
        scheduler.add_job(
            reverse_sync_test_tasks,
            "interval",
            seconds=settings.BITRIX24_REVERSE_SYNC_INTERVAL_SECONDS,
            id="bitrix24_reverse_sync",
            name="Bitrix24 Reverse Sync (GLPI -> Bitrix24)",
            max_instances=1,
        )
    if settings.ORG_SYNC_ENABLED and settings.BITRIX24_ORG_WEBHOOK_URL:
        scheduler.add_job(
            sync_org_structure,
            "interval",
            seconds=settings.ORG_SYNC_INTERVAL_SECONDS,
            id="bitrix24_org_sync",
            name="Bitrix24 Org Sync (users + departments)",
            max_instances=1,
        )


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
    _register_poller_jobs(_scheduler)
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


def _safe_next_run(job) -> str | None:
    """Return the job's next run time as ISO string, or None.

    APScheduler exposes ``next_run_time`` only while the scheduler is
    running — guard against a not-yet-started scheduler.
    """
    try:
        return job.next_run_time.isoformat()
    except (AttributeError, TypeError):
        return None


def get_poller_status() -> dict:
    """Get current poller status."""
    global _scheduler
    if _scheduler is None:
        return {"status": "not_started"}

    jobs = _scheduler.get_jobs()
    poll_job = next((j for j in jobs if j.id == "bitrix24_poll"), None)
    reverse_job = next(
        (j for j in jobs if j.id == "bitrix24_reverse_sync"), None
    )

    return {
        "status": "running",
        "interval_seconds": settings.BITRIX24_POLL_INTERVAL_SECONDS,
        "responsible_ids": settings.responsible_ids,
        "next_run": _safe_next_run(poll_job),
        "reverse_sync": {
            "enabled": reverse_job is not None,
            "auto_enabled": settings.BITRIX24_REVERSE_SYNC_ENABLED,
            "interval_seconds": settings.BITRIX24_REVERSE_SYNC_INTERVAL_SECONDS,
            "next_run": _safe_next_run(reverse_job),
        },
    }
