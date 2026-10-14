"""The mower's own blade counter on the health totals (Mammotion ``blade_used_time``).

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
    """Add the nullable ``device_blade_seconds`` and ``device_blade_warn_seconds``."""
    op.add_column("mower_health", sa.Column("device_blade_seconds", sa.Double(), nullable=True))
    op.add_column(
        "mower_health", sa.Column("device_blade_warn_seconds", sa.Double(), nullable=True)
    )


def downgrade() -> None:
    """Drop the device blade counter columns."""
    op.drop_column("mower_health", "device_blade_warn_seconds")
    op.drop_column("mower_health", "device_blade_seconds")
