"""Outbox model — transactional outbox for reliable message publishing."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Outbox(Base):
    """Transactional outbox entry.

    Messages are inserted atomically within the same DB transaction as the
    task mutation. A background worker polls for unpublished entries and
    forwards them to the message broker.

    Attributes:
        id:            UUID PK.
        task_id:       FK to the parent task.
        routing_key:   Target exchange / routing key (e.g. "tasks:pending:primary").
        payload:       Message body as JSONB.
        created_at:    Insertion timestamp.
        published_at:  Publication timestamp (NULL → not yet published).
        retry_count:   Number of publish retries so far.
        last_error:    Error message from the last failed publish attempt.
    """

    __tablename__ = "outbox"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    routing_key: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    retry_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=func.text("0"),
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
