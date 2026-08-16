"""add last_elapsed_synced and last_l1_hash to tasks

Revision ID: d0e1f2a3b4c5
Revises: c7d9e0f1a2b3
Create Date: 2026-08-16 03:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d0e1f2a3b4c5"
down_revision: str | None = "c7d9e0f1a2b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("last_elapsed_synced", sa.Integer(), nullable=True),
    )
    op.add_column(
        "tasks",
        sa.Column("last_l1_hash", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tasks", "last_l1_hash")
    op.drop_column("tasks", "last_elapsed_synced")
