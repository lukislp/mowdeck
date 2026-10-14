"""Device snapshots: the newest map and capabilities of every mower.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    """Create ``mower_snapshots``."""
    op.create_table(
        "mower_snapshots",
        sa.Column("mower_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("observed_at", TS, nullable=False),
        sa.Column("received_at", TS, nullable=False),
        sa.Column("revision", sa.String(128), nullable=False),
        sa.Column("body", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("mower_id", "kind", name="pk_mower_snapshots"),
    )
    op.create_index("ix_mower_snapshots_account", "mower_snapshots", ["account_id"])


def downgrade() -> None:
    """Drop ``mower_snapshots``."""
    op.drop_table("mower_snapshots")
