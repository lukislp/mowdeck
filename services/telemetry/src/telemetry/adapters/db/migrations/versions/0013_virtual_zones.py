"""Virtual zones (ADR 0070): the drawn zones, where each mower is, time per zone and day.

Additive only: three new tables.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    """Create ``virtual_zones``, ``zone_presence`` and ``zone_days``."""
    op.create_table(
        "virtual_zones",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("mower_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(60), nullable=False),
        sa.Column("ring", JSONB(), nullable=False),
        sa.Column("area_m2", sa.Double(), nullable=False),
        sa.Column("start_plan_ref", sa.String(100), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_virtual_zones"),
    )
    op.create_index("ix_virtual_zones_mower", "virtual_zones", ["mower_id", "created_at"])
    op.create_index("ix_virtual_zones_account", "virtual_zones", ["account_id"])

    op.create_table(
        "zone_presence",
        sa.Column("mower_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("zone_id", sa.Uuid(), nullable=True),
        sa.Column("since", TS, nullable=False),
        sa.Column("observed_at", TS, nullable=False),
        sa.Column("seen_at", TS, nullable=True),
        sa.Column("mowing", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("lat", sa.Double(), nullable=True),
        sa.Column("lon", sa.Double(), nullable=True),
        sa.Column("rtk_fix", sa.String(16), server_default="unknown", nullable=False),
        sa.PrimaryKeyConstraint("mower_id", name="pk_zone_presence"),
    )
    op.create_index("ix_zone_presence_account", "zone_presence", ["account_id"])

    op.create_table(
        "zone_days",
        sa.Column("mower_id", sa.Uuid(), nullable=False),
        sa.Column("zone_id", sa.Uuid(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("seconds", sa.Double(), server_default="0", nullable=False),
        sa.Column("mowing_seconds", sa.Double(), server_default="0", nullable=False),
        sa.Column("entries", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(
            ["zone_id"],
            ["virtual_zones.id"],
            name="fk_zone_days_zone_id_virtual_zones",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("mower_id", "zone_id", "day", name="pk_zone_days"),
    )
    op.create_index("ix_zone_days_zone", "zone_days", ["zone_id"])


def downgrade() -> None:
    """Drop what :func:`upgrade` added."""
    op.drop_table("zone_days")
    op.drop_table("zone_presence")
    op.drop_table("virtual_zones")
