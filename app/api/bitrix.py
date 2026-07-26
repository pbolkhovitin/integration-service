"""Bitrix24 sync API — status and manual trigger endpoints."""

import logging

from fastapi import APIRouter

from app.services.poller import get_poller_status, _poll_bitrix24

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/bitrix24", tags=["bitrix24"])


@router.get("/sync/status")
async def sync_status() -> dict:
    """Get the current status of the Bitrix24 polling service."""
    return get_poller_status()


@router.post("/sync/trigger")
async def sync_trigger() -> dict:
    """Manually trigger a Bitrix24 poll cycle.

    Useful for testing or forcing an immediate sync without waiting for
    the next scheduled interval.
    """
    logger.info("Manual sync triggered via API")
    await _poll_bitrix24()
    return {"status": "completed"}
