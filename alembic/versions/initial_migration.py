"""create tasks, task_attempts, outbox tables + taskstatus enum

Revision ID: 001
Revises:
Create Date: 2024-01-01 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Enum type ──────────────────────────────────────────────────────────
    op.execute(
        "CREATE TYPE taskstatus AS ENUM ("
        "'pending', 'processing', 'completed', 'failed', 'cancelled')"
    )

    # ── tasks table ────────────────────────────────────────────────────────
    op.create_table(
        "tasks",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("source_id", sa.String(255), nullable=False),
        sa.Column("type", sa.String(50), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(
                "pending",
                "processing",
                "completed",
                "failed",
                "cancelled",
                name="taskstatus",
                create_type=False,
            ),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "max_attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("3"),
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("idempotency_key", sa.String(255), nullable=True),
        sa.Column("worker_id", sa.String(100), nullable=True),
        sa.Column(
            "lease_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    # Task indexes
    op.create_index("ix_tasks_status", "tasks", ["status"])
    op.create_index("ix_tasks_source", "tasks", ["source"])
    op.create_index(
        "ix_tasks_idempotency_key",
        "tasks",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_index(
        "ix_tasks_lease",
        "tasks",
        ["worker_id", "lease_expires_at"],
    )

    # ── task_attempts table ────────────────────────────────────────────────
    op.create_table(
        "task_attempts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "task_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column(
            "status_before",
            postgresql.ENUM(
                "pending",
                "processing",
                "completed",
                "failed",
                "cancelled",
                name="taskstatus",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "status_after",
            postgresql.ENUM(
                "pending",
                "processing",
                "completed",
                "failed",
                "cancelled",
                name="taskstatus",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_task_attempts_task_id",
        "task_attempts",
        ["task_id"],
    )

    # ── outbox table ───────────────────────────────────────────────────────
    op.create_table(
        "outbox",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "task_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("routing_key", sa.String(100), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "retry_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_outbox_unpublished",
        "outbox",
        ["published_at"],
        postgresql_where=sa.text("published_at IS NULL"),
    )


def downgrade() -> None:
    # Drop indexes first
    op.drop_index("ix_outbox_unpublished", table_name="outbox")
    op.drop_index("ix_task_attempts_task_id", table_name="task_attempts")
    op.drop_index("ix_tasks_lease", table_name="tasks")
    op.drop_index("ix_tasks_idempotency_key", table_name="tasks")
    op.drop_index("ix_tasks_source", table_name="tasks")
    op.drop_index("ix_tasks_status", table_name="tasks")

    # Drop tables (order matters: child tables first)
    op.drop_table("outbox")
    op.drop_table("task_attempts")
    op.drop_table("tasks")

    # Drop enum type last (no tables reference it any more)
    op.execute("DROP TYPE taskstatus")
