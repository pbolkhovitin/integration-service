"""add last_b24_comment_id and mirrored_followups table

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-08-16 14:50:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e1f2a3b4c5d6"
down_revision: str | None = "d0e1f2a3b4c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("last_b24_comment_id", sa.Integer(), nullable=True),
    )
    op.create_table(
        "mirrored_followups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("glpi_followup_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "glpi_followup_id", name="ix_mirrored_followups_glpi_followup_id"
        ),
    )


def downgrade() -> None:
    op.drop_table("mirrored_followups")
    op.drop_column("tasks", "last_b24_comment_id")
