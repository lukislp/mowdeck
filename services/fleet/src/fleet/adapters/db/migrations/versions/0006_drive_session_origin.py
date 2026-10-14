"""The origin of a drive session (Vision drives on behalf of a user, ADR 0033).

Additive: one nullable column, so a running older Fleet keeps working during the rollout.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``drive_sessions.origin``."""
    op.add_column("drive_sessions", sa.Column("origin", sa.String(32), nullable=True))


def downgrade() -> None:
    """Drop the column again."""
    op.drop_column("drive_sessions", "origin")
