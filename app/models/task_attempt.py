"""TaskAttempt model — append-only audit log for every task execution attempt."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Text, Enum as SAEnum, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.task import TaskStatus


class TaskAttempt(Base):
    """Records every attempt to execute a task (append-only).

    Attributes:
        id:              Auto-incrementing PK.
        task_id:         FK to the parent task.
        attempt_number:  Sequential attempt number (1-based).
        status_before:   Task status before this attempt.
        status_after:    Task status after this attempt.
        error:           Error message if the attempt failed.
        started_at:      When the attempt started.
        completed_at:    When the attempt finished (nullable = still running).
        metadata:        Arbitrary additional data (worker info, retry delay, …).
    """

    __tablename__ = "task_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)

    status_before: Mapped[TaskStatus] = mapped_column(
        SAEnum(TaskStatus, name="taskstatus", create_constraint=True),
        nullable=False,
    )
    status_after: Mapped[TaskStatus] = mapped_column(
        SAEnum(TaskStatus, name="taskstatus", create_constraint=True),
        nullable=False,
    )

    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", JSONB, nullable=True  # DB column name = "metadata"
    )
