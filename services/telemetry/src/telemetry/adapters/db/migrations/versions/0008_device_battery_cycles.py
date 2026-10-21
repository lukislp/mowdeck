"""The mower's own lifetime battery cycle counter on the health totals (Mammotion ``bat_cycles``).

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable ``device_battery_cycles``."""
    op.add_column("mower_health", sa.Column("device_battery_cycles", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Drop the device battery cycle counter column."""
    op.drop_column("mower_health", "device_battery_cycles")
