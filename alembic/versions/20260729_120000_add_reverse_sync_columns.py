"""add reverse sync columns

Revision ID: 2f8a1c3b5e7d
Revises: 001
Create Date: 2026-07-29 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "2f8a1c3b5e7d"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("last_glpi_status", sa.String(50), nullable=True))
    op.add_column("tasks", sa.Column("last_glpi_followup_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "last_glpi_followup_id")
    op.drop_column("tasks", "last_glpi_status")
