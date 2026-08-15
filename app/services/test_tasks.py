"""Runtime Bitrix24 test-task whitelist management.

The whitelist of Bitrix24 task IDs allowed for WRITE operations (reverse
sync / L1 write-back) combines:
- the static baseline from ``TEST_TASK_IDS`` env,
- runtime additions stored in ``bitrix_test_tasks`` (added via the admin
  API right after a new test task is created in Bitrix24).

During development/testing ALL other Bitrix24 tasks are read-only.
"""

import logging

from sqlalchemy import select

from app.config.settings import settings
from app.core.database import async_session_factory
from app.models.test_task import BitrixTestTask

logger = logging.getLogger(__name__)


def env_test_task_ids() -> set[int]:
    """Return the static (env) test task IDs."""
    return set(settings.test_task_ids)


async def db_test_task_ids() -> set[int]:
    """Return runtime test task IDs stored in the database."""
    async with async_session_factory() as db:
        result = await db.execute(select(BitrixTestTask.task_id))
        return {row[0] for row in result.all()}


async def allowed_test_task_ids() -> set[int]:
    """Full writable whitelist: env baseline + runtime DB additions."""
    return env_test_task_ids() | await db_test_task_ids()


async def add_test_task(task_id: int) -> dict:
    """Add a Bitrix24 task ID to the writable whitelist."""
    async with async_session_factory() as db:
        exists = await db.execute(
            select(BitrixTestTask.id).where(BitrixTestTask.task_id == task_id)
        )
        if exists.scalar_one_or_none() is not None:
            return {"task_id": task_id, "added": False, "already_present": True}
        db.add(BitrixTestTask(task_id=task_id, source="manual"))
        await db.commit()
    logger.info("Added Bitrix24 test task %s to writable whitelist", task_id)
    return {"task_id": task_id, "added": True}


async def remove_test_task(task_id: int) -> dict:
    """Remove a Bitrix24 task ID from the runtime whitelist."""
    async with async_session_factory() as db:
        result = await db.execute(
            select(BitrixTestTask).where(BitrixTestTask.task_id == task_id)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return {"task_id": task_id, "removed": False}
        await db.delete(row)
        await db.commit()
    logger.info("Removed Bitrix24 test task %s from writable whitelist", task_id)
    return {"task_id": task_id, "removed": True}


async def list_test_tasks() -> dict:
    """Return the full writable whitelist (env + DB) for status/reporting."""
    env_ids = sorted(env_test_task_ids())
    db_ids = sorted(await db_test_task_ids())
    return {
        "env_task_ids": env_ids,
        "runtime_task_ids": db_ids,
        "allowed_task_ids": sorted(set(env_ids) | set(db_ids)),
    }
