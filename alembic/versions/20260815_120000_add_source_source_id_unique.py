"""enforce uniqueness on (source, source_id)

Revision ID: a1b2c3d4e5f6
Revises: 2f8a1c3b5e7d
Create Date: 2026-08-15 12:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "2f8a1c3b5e7d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Deduplicate (source, source_id): prefer 'completed', otherwise keep
    # the oldest row (earliest created_at, then lowest id). Runs before the
    # unique index so the migration succeeds even on dirty data.
    op.execute(
        """
        DELETE FROM tasks a
        USING tasks b
        WHERE a.source = b.source
          AND a.source_id = b.source_id
          AND a.id <> b.id
          AND (
                (a.status <> 'completed' AND b.status = 'completed')
             OR (a.status = b.status AND a.created_at > b.created_at)
             OR (a.status = b.status AND a.created_at = b.created_at AND a.id > b.id)
          )
        """
    )
    op.create_index(
        "ix_tasks_source_source_id",
        "tasks",
        ["source", "source_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_tasks_source_source_id", table_name="tasks")