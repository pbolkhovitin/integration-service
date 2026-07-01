"""Bitrix24 webhook endpoints — MVP synchronous processing."""

import asyncio
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.core.database import get_db
from app.models.task import Task, TaskStatus
from app.services.glpi import GLPIClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook/bitrix", tags=["bitrix24"])


class BitrixLead(BaseModel):
    """Incoming lead from Bitrix24 webhook."""

    name: str = Field(..., min_length=1, description="Lead name / title")
    phone: str | None = None
    email: str | None = None
    offer_content: str | None = None
    idempotency_key: str | None = Field(
        None, max_length=255, description="Idempotency key to avoid duplicate tickets"
    )


def _build_ticket_content(lead: BitrixLead) -> str:
    """Build the GLPI ticket description from a lead."""
    lines = [
        f"[Lead: {lead.name}]",
        f"Phone: {lead.phone or 'N/A'}",
        f"Email: {lead.email or 'N/A'}",
        f"Offer: {lead.offer_content or 'N/A'}",
    ]
    return "\n".join(lines)


@router.post("/lead")
async def receive_lead(
    lead: BitrixLead,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Receive a Bitrix24 lead and create a GLPI ticket synchronously.

    Supports optional idempotency via ``idempotency_key`` to prevent
    duplicate GLPI ticket creation.
    """
    logger.info("Incoming lead: %s", lead.name)

    # ------------------------------------------------------------------
    # Idempotency check
    # ------------------------------------------------------------------
    if lead.idempotency_key:
        result = await db.execute(
            select(Task).where(Task.idempotency_key == lead.idempotency_key)
        )
        existing: Task | None = result.scalar_one_or_none()
        if existing is not None:
            if existing.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                return {
                    "status": "duplicate",
                    "task_id": str(existing.id),
                }
            # Task is still pending / processing
            return {
                "status": "in_progress",
                "task_id": str(existing.id),
            }

    # ------------------------------------------------------------------
    # Build ticket content
    # ------------------------------------------------------------------
    ticket_content = _build_ticket_content(lead)

    # ------------------------------------------------------------------
    # Create Task record
    # ------------------------------------------------------------------
    task = Task(
        source="bitrix24",
        source_id=lead.idempotency_key or str(uuid.uuid4()),
        type="create_ticket",
        payload=lead.model_dump(),
        status=TaskStatus.PROCESSING,
        idempotency_key=lead.idempotency_key,
    )
    db.add(task)
    await db.commit()

    # ------------------------------------------------------------------
    # Sync GLPI calls via asyncio.to_thread()
    # ------------------------------------------------------------------
    glpi_client = None
    try:
        glpi_client = GLPIClient(
            base_url=settings.GLPI_URL,
            app_token=settings.GLPI_APP_TOKEN.get_secret_value(),
            user_token=settings.GLPI_USER_TOKEN.get_secret_value(),
        )

        session_token = await asyncio.to_thread(glpi_client.init_session)
        ticket = await asyncio.to_thread(
            glpi_client.create_ticket,
            name=lead.name,
            content=ticket_content,
            session_token=session_token,
        )
    except Exception as exc:
        logger.exception("GLPI processing failed for lead %s", lead.name)
        task.status = TaskStatus.FAILED
        task.last_error = str(exc)
        await db.commit()
        raise HTTPException(
            status_code=500,
            detail="GLPI processing failed",
        ) from exc
    finally:
        if glpi_client is not None:
            glpi_client.close()

    # ------------------------------------------------------------------
    # Mark task as completed
    # ------------------------------------------------------------------
    task.status = TaskStatus.COMPLETED
    task.result = ticket
    await db.commit()

    return {
        "status": "success",
        "task_id": str(task.id),
        "glpi_ticket": ticket,
    }
