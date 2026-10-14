"""Telemetry schema: mower directory, latest state, history, position track, command timeline.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    """Create the tables and their indexes."""
    op.create_table(
        "mower_directory",
        sa.Column("mower_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("mower_id", name="pk_mower_directory"),
    )
    op.create_index("ix_mower_directory_account", "mower_directory", ["account_id"])

    op.create_table(
        "mower_latest",
        sa.Column("mower_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("observed_at", TS, nullable=False),
        sa.Column("received_at", TS, nullable=False),
        sa.Column("activity", sa.String(32), nullable=False),
        sa.Column("battery_percent", sa.SmallInteger(), nullable=False),
        sa.Column("state", postgresql.JSONB(), nullable=False),
        sa.Column("history_written_at", TS, nullable=True),
        sa.Column("history_fingerprint", sa.String(256), nullable=True),
        sa.Column("position_fingerprint", sa.String(128), nullable=True),
        sa.PrimaryKeyConstraint("mower_id", name="pk_mower_latest"),
    )
    op.create_index("ix_mower_latest_account", "mower_latest", ["account_id"])

    op.create_table(
        "mower_history",
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("mower_id", sa.Uuid(), nullable=False),
        sa.Column("observed_at", TS, nullable=False),
        sa.Column("activity", sa.String(32), nullable=False),
        sa.Column("battery_percent", sa.SmallInteger(), nullable=False),
        sa.Column("charging", sa.Boolean(), nullable=False),
        sa.Column("rtk_fix", sa.String(16), nullable=False),
        sa.Column("connection", sa.String(32), nullable=False),
        sa.Column("blade_height_mm", sa.SmallInteger(), nullable=True),
        sa.Column("speed_mps", sa.Float(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.PrimaryKeyConstraint("mower_id", "observed_at", name="pk_mower_history"),
    )
    op.create_index(
        "ix_mower_history_mower_time", "mower_history", ["mower_id", sa.text("observed_at DESC")]
    )
    op.create_index(
        "ix_mower_history_account_time",
        "mower_history",
        ["account_id", sa.text("observed_at DESC")],
    )
    op.create_index(
        "ix_mower_history_observed_brin", "mower_history", ["observed_at"], postgresql_using="brin"
    )

    op.create_table(
        "mower_positions",
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("mower_id", sa.Uuid(), nullable=False),
        sa.Column("observed_at", TS, nullable=False),
        sa.Column("lat", sa.Double(), nullable=False),
        sa.Column("lon", sa.Double(), nullable=False),
        sa.Column("alt", sa.Double(), nullable=True),
        sa.Column("heading", sa.Float(), nullable=True),
        sa.Column("rtk_fix", sa.String(16), nullable=False),
        sa.Column("rtk_source", sa.String(16), nullable=False),
        sa.PrimaryKeyConstraint("mower_id", "observed_at", name="pk_mower_positions"),
    )
    op.create_index(
        "ix_mower_positions_mower_time",
        "mower_positions",
        ["mower_id", sa.text("observed_at DESC")],
    )
    op.create_index(
        "ix_mower_positions_account_time",
        "mower_positions",
        ["account_id", sa.text("observed_at DESC")],
    )
    op.create_index(
        "ix_mower_positions_observed_brin",
        "mower_positions",
        ["observed_at"],
        postgresql_using="brin",
    )

    op.create_table(
        "command_timeline",
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("mower_id", sa.Uuid(), nullable=False),
        sa.Column("command_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(500), nullable=True),
        sa.Column("at", TS, nullable=False),
        sa.PrimaryKeyConstraint("command_id", "status", name="pk_command_timeline"),
    )
    op.create_index(
        "ix_command_timeline_mower_time", "command_timeline", ["mower_id", sa.text("at DESC")]
    )
    op.create_index(
        "ix_command_timeline_account_time", "command_timeline", ["account_id", sa.text("at DESC")]
    )
    op.create_index(
        "ix_command_timeline_at_brin", "command_timeline", ["at"], postgresql_using="brin"
    )


def downgrade() -> None:
    """Drop the tables (their indexes go with them)."""
    for table in (
        "command_timeline",
        "mower_positions",
        "mower_history",
        "mower_latest",
        "mower_directory",
    ):
        op.drop_table(table)
