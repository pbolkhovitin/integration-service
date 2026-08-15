"""Bitrix24 test-task whitelist (writable tasks during development)."""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class BitrixTestTask(Base):
    """A Bitrix24 task ID allowed for write operations during dev/test.

    The static baseline comes from ``TEST_TASK_IDS`` env; this table holds
    additional task IDs added at runtime (e.g. right after creating a new
    test task in Bitrix24) without redeploying.
    """

    __tablename__ = "bitrix_test_tasks"

    __table_args__ = (
        UniqueConstraint("task_id", name="ix_bitrix_test_tasks_task_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    source: Mapped[str] = mapped_column(
        String(50), nullable=False, server_default="manual"
    )
