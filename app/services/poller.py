"""APScheduler-based poller for Bitrix24 tasks.

Periodically polls Bitrix24 REST API for new/updated tasks and creates
corresponding GLPI tickets. Runs inside the FastAPI process (no Celery needed).
"""

import asyncio
import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from app.config.settings import settings
from app.core.database import async_session_factory
from app.models.task import Task
from app.services.bitrix import BitrixClient
from app.services.glpi import GLPIClient

logger = logging.getLogger(__name__)

# Global scheduler instance
_scheduler: AsyncIOScheduler | None = None


async def _poll_bitrix24() -> None:
    """Poll Bitrix24 for tasks and create GLPI tickets.

    This is the core polling job that runs on schedule. It:
    1. Fetches tasks from Bitrix24 for each responsible_id
    2. Checks idempotency (skip already processed tasks)
    3. Creates GLPI tickets for new tasks
    4. Records results in the database
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

    try:
        # Init GLPI session
        glpi_session = glpi_client.init_session()
        logger.debug("GLPI session initialized: %s...", glpi_session[:8])

        for responsible_id in responsible_ids:
            await _poll_for_user(
                bitrix_client=bitrix_client,
                glpi_client=glpi_client,
                glpi_session=glpi_session,
                responsible_id=responsible_id,
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
) -> None:
    """Poll Bitrix24 for a single user's tasks."""
    start = 0
    total_processed = 0
    total_skipped = 0

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
        "User %d: processed %d, skipped %d",
        responsible_id,
        total_processed,
        total_skipped,
    )


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
