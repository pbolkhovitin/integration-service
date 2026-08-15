"""add bitrix_test_tasks whitelist table

Revision ID: b4f3a1c2d5e6
Revises: a1b2c3d4e5f6
Create Date: 2026-08-15 14:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b4f3a1c2d5e6"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "bitrix_test_tasks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "source", sa.String(length=50), server_default="manual",
            nullable=False,
        ),
        sa.UniqueConstraint("task_id", name="ix_bitrix_test_tasks_task_id"),
    )


def downgrade() -> None:
    op.drop_table("bitrix_test_tasks")
