"""Site coordinates (rain pause of the Scheduler) and the origin of issued commands.

Additive: nullable columns only, so a running older Fleet keeps working during the rollout.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``sites.latitude``/``longitude`` and ``commands.origin``/``schedule_id``."""
    op.add_column("sites", sa.Column("latitude", sa.Float(), nullable=True))
    op.add_column("sites", sa.Column("longitude", sa.Float(), nullable=True))
    op.create_check_constraint(
        "ck_sites_coordinates",
        "sites",
        "(latitude IS NULL) = (longitude IS NULL)"
        " AND (latitude IS NULL OR latitude BETWEEN -90 AND 90)"
        " AND (longitude IS NULL OR longitude BETWEEN -180 AND 180)",
    )
    op.add_column("commands", sa.Column("origin", sa.String(32), nullable=True))
    op.add_column("commands", sa.Column("schedule_id", sa.Uuid(), nullable=True))


def downgrade() -> None:
    """Drop the columns again."""
    op.drop_column("commands", "schedule_id")
    op.drop_column("commands", "origin")
    op.drop_constraint("ck_sites_coordinates", "sites", type_="check")
    op.drop_column("sites", "longitude")
    op.drop_column("sites", "latitude")
