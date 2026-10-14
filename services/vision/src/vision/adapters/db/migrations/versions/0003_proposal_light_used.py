"""Proposals remember whether the photo was taken with the mower's light on.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``proposals.light_used`` (false for every existing row)."""
    op.add_column(
        "proposals",
        sa.Column("light_used", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )


def downgrade() -> None:
    """Drop ``proposals.light_used``."""
    op.drop_column("proposals", "light_used")
