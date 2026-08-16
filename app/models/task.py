"""Task model — the central work unit for the integration service."""

import enum
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin


class TaskStatus(str, enum.Enum):
    """Lifecycle status for a task."""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Task(Base, TimestampMixin):
    """Represents a single unit of integration work.

    Attributes:
        source:         Source system identifier (Bitrix24 / MANGO / GLPI).
        source_id:      Original object ID in the source system.
        type:           Task type (create_ticket, register_call, transcribe, …).
        payload:        Arbitrary input data as JSONB.
        status:         Current lifecycle status (see TaskStatus enum).
        attempts:       Number of execution attempts so far.
        max_attempts:   Maximum allowed attempts before giving up.
        last_error:     Human-readable error message from the last failed attempt.
        result:         Successful execution result as JSONB.
        idempotency_key: Unique key for webhook idempotency (unique when set).
        worker_id:      ID of the worker that currently holds the lease.
        lease_expires_at: Timestamp after which the lease is considered stale.
    """

    __tablename__ = "tasks"

    __table_args__ = (
        UniqueConstraint("source", "source_id", name="ix_tasks_source_source_id"),
    )

    source: Mapped[str] = mapped_column(String(50), nullable=False)
    source_id: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    status: Mapped[str] = mapped_column(
        SAEnum(
            "pending", "processing", "completed", "failed", "cancelled",
            name="taskstatus",
            create_constraint=True,
        ),
        nullable=False,
        default="pending",
        server_default=func.text("'pending'"),
    )

    attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=func.text("0"),
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=3,
        server_default=func.text("3"),
    )

    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    idempotency_key: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        # NOTE: uniqueness is enforced via a partial unique index
        # (ix_tasks_idempotency_key WHERE idempotency_key IS NOT NULL)
        # to allow multiple NULL values.
    )
    worker_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_glpi_status: Mapped[str | None] = mapped_column(
        String(50), nullable=True, default=None
    )
    last_glpi_followup_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=None
    )
    last_elapsed_synced: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=None
    )
    last_l1_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True, default=None
    )
    last_b24_comment_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=None
    )
