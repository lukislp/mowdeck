"""The mower's own lifetime working time and distance on the health totals (Mammotion
``maintenance.work_time`` and ``maintenance.mileage``).

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable ``device_mowing_seconds`` and ``device_distance_m``."""
    op.add_column("mower_health", sa.Column("device_mowing_seconds", sa.Double(), nullable=True))
    op.add_column("mower_health", sa.Column("device_distance_m", sa.Double(), nullable=True))


def downgrade() -> None:
    """Drop the device lifetime totals."""
    op.drop_column("mower_health", "device_distance_m")
    op.drop_column("mower_health", "device_mowing_seconds")
