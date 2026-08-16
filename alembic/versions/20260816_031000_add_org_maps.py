"""add org_user_map and org_department_map mapping tables

Revision ID: c7d9e0f1a2b3
Revises: b4f3a1c2d5e6
Create Date: 2026-08-16 03:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7d9e0f1a2b3"
down_revision: str | None = "b4f3a1c2d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "org_user_map",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("b24_user_id", sa.Integer(), nullable=False),
        sa.Column("glpi_user_id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False, server_default=""),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("b24_user_id", name="ix_org_user_map_b24_user_id"),
    )
    op.create_table(
        "org_department_map",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("b24_dept_id", sa.Integer(), nullable=False),
        sa.Column("glpi_entity_id", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("b24_dept_id", name="ix_org_department_map_b24_dept_id"),
    )


def downgrade() -> None:
    op.drop_table("org_department_map")
    op.drop_table("org_user_map")
