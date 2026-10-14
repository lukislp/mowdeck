"""Health history: running totals, UTC day totals and blade changes per mower.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    """Create ``mower_health``, ``mower_health_daily`` and ``blade_changes``."""
    op.create_table(
        "mower_health",
        sa.Column("mower_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("since", TS, nullable=True),
        sa.Column("blade_seconds", sa.Double(), nullable=False),
        sa.Column("mowing_seconds", sa.Double(), nullable=False),
        sa.Column("distance_m", sa.Double(), nullable=False),
        sa.Column("battery_cycles", sa.Integer(), nullable=False),
        sa.Column("charge_sessions", sa.Integer(), nullable=False),
        sa.Column("errors", postgresql.JSONB(), nullable=False),
        sa.Column("blade_seconds_since_change", sa.Double(), nullable=False),
        sa.Column("blade_changed_at", TS, nullable=True),
        sa.Column("blade_threshold_notified", sa.Boolean(), nullable=False),
        sa.Column("last_observed_at", TS, nullable=True),
        sa.Column("last_activity", sa.String(32), nullable=True),
        sa.Column("last_charging", sa.Boolean(), nullable=True),
        sa.Column("last_blade_on", sa.Boolean(), nullable=True),
        sa.Column("last_lat", sa.Double(), nullable=True),
        sa.Column("last_lon", sa.Double(), nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column("battery_armed", sa.Boolean(), nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("mower_id", name="pk_mower_health"),
    )
    op.create_index("ix_mower_health_account", "mower_health", ["account_id"])

    op.create_table(
        "mower_health_daily",
        sa.Column("mower_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("blade_seconds", sa.Double(), nullable=False),
        sa.Column("mowing_seconds", sa.Double(), nullable=False),
        sa.Column("distance_m", sa.Double(), nullable=False),
        sa.Column("battery_cycles", sa.Integer(), nullable=False),
        sa.Column("charge_sessions", sa.Integer(), nullable=False),
        sa.Column("errors", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("mower_id", "day", name="pk_mower_health_daily"),
    )

    op.create_table(
        "blade_changes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("mower_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("changed_at", TS, nullable=False),
        sa.Column("note", sa.String(500), nullable=True),
        sa.Column("changed_by", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_blade_changes"),
    )
    op.create_index(
        "ix_blade_changes_mower_time", "blade_changes", ["mower_id", sa.text("changed_at DESC")]
    )


def downgrade() -> None:
    """Drop the health tables."""
    op.drop_table("blade_changes")
    op.drop_table("mower_health_daily")
    op.drop_table("mower_health")
