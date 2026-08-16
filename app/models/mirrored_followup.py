"""GLPI followups created FROM Bitrix24 task comments (mirror loop-protection)."""

from datetime import datetime

from sqlalchemy import DateTime, Integer, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class MirroredFollowup(Base):
    """Tracks GLPI followup IDs that were mirrored from Bitrix24 comments.

    Reverse sync skips these followups so they are NOT written back to
    Bitrix24 (prevents comment loops).
    """

    __tablename__ = "mirrored_followups"

    __table_args__ = (
        UniqueConstraint(
            "glpi_followup_id", name="ix_mirrored_followups_glpi_followup_id"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(Integer, nullable=False)
    glpi_followup_id: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
