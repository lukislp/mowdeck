"""Watermark table of the position downsampling (ADR 0053).

The slice queries use the existing indexes ``pk_mower_positions`` and
``ix_mower_positions_mower_time`` (``mower_id, observed_at``), so no index is added.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``mower_positions_thinning``."""
    op.create_table(
        "mower_positions_thinning",
        sa.Column("mower_id", sa.Uuid(), nullable=False),
        sa.Column("thinned_until", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("mower_id", name=op.f("pk_mower_positions_thinning")),
    )


def downgrade() -> None:
    """Drop it (thinned rows stay thinned; the next upgrade starts over and is idempotent)."""
    op.drop_table("mower_positions_thinning")
