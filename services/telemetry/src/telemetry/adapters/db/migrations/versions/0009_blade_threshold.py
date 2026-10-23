"""A blade hours threshold per mower on the health totals (null: the configured default).

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable ``blade_threshold_seconds``."""
    op.add_column("mower_health", sa.Column("blade_threshold_seconds", sa.Double(), nullable=True))


def downgrade() -> None:
    """Drop the per-mower blade threshold column."""
    op.drop_column("mower_health", "blade_threshold_seconds")
