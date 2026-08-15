"""Bitrix24 sync API — status, manual trigger, and cleanup endpoints."""

import logging

from fastapi import APIRouter, Depends, Header, HTTPException

from app.config.settings import settings
from app.services.org_sync import (
    get_org_sync_status,
    sync_org_structure,
)
from app.services.poller import (
    _poll_bitrix24,
    cleanup_orphaned_tasks,
    get_poller_status,
    retry_failed_tasks,
)
from app.services.reverse_sync import (
    get_reverse_sync_status,
    reverse_sync_test_tasks,
)
from app.services.test_tasks import (
    add_test_task,
    list_test_tasks,
    remove_test_task,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/bitrix24", tags=["bitrix24"])


def require_admin_token(x_admin_token: str = Header(default="")) -> None:
    """Reject requests without the configured admin token.

    Mutating endpoints (trigger, cleanup, retry, reverse-test) change
    external state (GLPI/Bitrix24) and must be protected by the shared
    secret from ``ADMIN_API_TOKEN``. When the token is not configured the
    endpoints are disabled (always 401).
    """
    expected = settings.ADMIN_API_TOKEN.get_secret_value()
    if not expected or x_admin_token != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing admin token")


@router.get("/sync/status")
async def sync_status() -> dict:
    """Get the current status of the Bitrix24 polling service."""
    return get_poller_status()


@router.post(
    "/sync/trigger",
    dependencies=[Depends(require_admin_token)],
)
async def sync_trigger() -> dict:
    """Manually trigger a Bitrix24 poll cycle.

    Useful for testing or forcing an immediate sync without waiting for
    the next scheduled interval.
    """
    logger.info("Manual sync triggered via API")
    await _poll_bitrix24()
    return {"status": "completed"}


@router.post(
    "/sync/cleanup",
    dependencies=[Depends(require_admin_token)],
)
async def sync_cleanup() -> dict:
    """Detect tasks deleted in Bitrix24 and close their GLPI tickets.

    Fetches all current tasks from Bitrix24, compares against our DB,
    and updates GLPI ticket status to 'solved' for orphaned records.
    """
    logger.info("Manual cleanup triggered via API")
    result = await cleanup_orphaned_tasks()
    return result


@router.post(
    "/sync/retry",
    dependencies=[Depends(require_admin_token)],
)
async def sync_retry() -> dict:
    """Requeue failed bitrix24 tasks for the next poll cycle."""
    logger.info("Manual retry triggered via API")
    return await retry_failed_tasks()


@router.get("/sync/reverse-status")
async def reverse_sync_status() -> dict:
    """Get reverse sync configuration status."""
    return get_reverse_sync_status()


@router.post(
    "/sync/reverse-test",
    dependencies=[Depends(require_admin_token)],
)
async def reverse_sync_trigger() -> dict:
    """Manually trigger reverse sync for test tasks.

    Checks GLPI tickets for status changes and new followups,
    then updates Bitrix24 tasks accordingly.
    Only affects whitelisted test task IDs when TEST_MODE=True.
    """
    logger.info("Manual reverse sync triggered via API")
    result = await reverse_sync_test_tasks()
    return result


@router.get("/sync/org-status")
async def org_sync_status() -> dict:
    """Get org sync (users/departments) configuration status."""
    return get_org_sync_status()


@router.get("/sync/test-tasks")
async def test_tasks_list() -> dict:
    """List the writable Bitrix24 task whitelist (env + runtime)."""
    return await list_test_tasks()


@router.post(
    "/sync/test-tasks",
    dependencies=[Depends(require_admin_token)],
)
async def test_tasks_add(payload: dict) -> dict:
    """Add a Bitrix24 task ID to the writable whitelist.

    Use right after creating a new test task in Bitrix24 — it then becomes
    writable (L1 write-back / reverse sync) during development.
    """
    task_id = int(payload.get("task_id", 0))
    if task_id <= 0:
        raise HTTPException(status_code=422, detail="task_id required")
    return await add_test_task(task_id)


@router.delete(
    "/sync/test-tasks/{task_id}",
    dependencies=[Depends(require_admin_token)],
)
async def test_tasks_remove(task_id: int) -> dict:
    """Remove a Bitrix24 task ID from the runtime writable whitelist."""
    return await remove_test_task(task_id)


@router.post(
    "/sync/org",
    dependencies=[Depends(require_admin_token)],
)
async def org_sync_trigger() -> dict:
    """Mirror Bitrix24 departments and users into GLPI.

    Requires ``BITRIX24_ORG_WEBHOOK_URL`` (webhook with ``user`` and
    ``department`` scopes) and a GLPI API user with rights to create
    entities and users.
    """
    logger.info("Manual org sync triggered via API")
    result = await sync_org_structure()
    return result
