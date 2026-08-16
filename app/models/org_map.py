"""Bidirectional Bitrix24 ↔ GLPI mapping tables for org sync.

Filled by org sync; used by ticket creation to resolve the GLPI user/entity
for a given Bitrix24 user/department WITHOUT depending on email/name matching.
"""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class OrgUserMap(Base):
    """b24_user_id ↔ glpi_user_id (+ email) for requester/assignee resolution."""

    __tablename__ = "org_user_map"

    __table_args__ = (
        UniqueConstraint("b24_user_id", name="ix_org_user_map_b24_user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    b24_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    glpi_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class OrgDepartmentMap(Base):
    """b24_dept_id ↔ glpi_entity_id for entity resolution and re-parenting."""

    __tablename__ = "org_department_map"

    __table_args__ = (
        UniqueConstraint("b24_dept_id", name="ix_org_department_map_b24_dept_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    b24_dept_id: Mapped[int] = mapped_column(Integer, nullable=False)
    glpi_entity_id: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
