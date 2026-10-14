"""Day totals for reports: jobs started and online time per mower and UTC day.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``jobs`` and ``online_seconds`` to ``mower_health_daily`` (existing days: 0)."""
    op.add_column(
        "mower_health_daily",
        sa.Column("jobs", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "mower_health_daily",
        sa.Column("online_seconds", sa.Double(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    """Drop the report columns."""
    op.drop_column("mower_health_daily", "online_seconds")
    op.drop_column("mower_health_daily", "jobs")
