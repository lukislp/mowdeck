"""Signal strengths and the blade state on the position track (heatmaps).

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable ``wifi_rssi``, ``mnet_rssi`` (dBm) and ``blades_on``; older rows keep
    nulls (no rewrite of the table)."""
    op.add_column("mower_positions", sa.Column("wifi_rssi", sa.SmallInteger(), nullable=True))
    op.add_column("mower_positions", sa.Column("mnet_rssi", sa.SmallInteger(), nullable=True))
    op.add_column("mower_positions", sa.Column("blades_on", sa.Boolean(), nullable=True))


def downgrade() -> None:
    """Drop the signal and blade columns."""
    op.drop_column("mower_positions", "blades_on")
    op.drop_column("mower_positions", "mnet_rssi")
    op.drop_column("mower_positions", "wifi_rssi")
