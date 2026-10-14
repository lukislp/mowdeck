"""The hash of a spot plan sent with ``start_mow`` (Planner, ADR 0030).

Additive: a nullable column only, so a running older Fleet keeps working during the rollout.

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
    """Add ``commands.plan_hash``."""
    op.add_column("commands", sa.Column("plan_hash", sa.String(64), nullable=True))


def downgrade() -> None:
    """Drop the column again."""
    op.drop_column("commands", "plan_hash")
