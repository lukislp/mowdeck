"""Mowing defaults per mower (pattern, angle, border laps, spacing of a native start).

Additive: one nullable JSONB column on ``mowers``, so a running older Fleet keeps working
during the rollout (it neither reads nor writes the column).

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``mowers.mow_defaults``."""
    op.add_column("mowers", sa.Column("mow_defaults", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    """Drop the column again."""
    op.drop_column("mowers", "mow_defaults")
