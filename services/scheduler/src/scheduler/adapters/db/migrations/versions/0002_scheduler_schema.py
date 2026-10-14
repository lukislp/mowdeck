"""Scheduler schema: schedules, their runs, and the per-mower facts (latest activity, map).

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
EMPTY_LIST = sa.text("'[]'::jsonb")


def upgrade() -> None:
    """Create the tables and indexes."""
    op.create_table(
        "schedules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("target_type", sa.String(16), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("days_of_week", postgresql.JSONB(), server_default=EMPTY_LIST, nullable=False),
        sa.Column("anchor_date", sa.Date(), nullable=True),
        sa.Column("interval_days", sa.Integer(), nullable=True),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("timezone_override", sa.String(64), nullable=True),
        sa.Column("area_ids", postgresql.JSONB(), server_default=EMPTY_LIST, nullable=False),
        sa.Column("pattern", postgresql.JSONB(), nullable=True),
        sa.Column("rotation_angles", postgresql.JSONB(), server_default=EMPTY_LIST, nullable=False),
        sa.Column("rotation_index", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "rain", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "pause_running_jobs_on_rain",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "return_to_dock_at_window_end",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("disabled_reason", sa.String(500), nullable=True),
        sa.Column("permission_skips", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("skip_until", TS, nullable=True),
        sa.Column("owner", postgresql.JSONB(), nullable=False),
        sa.Column("owner_subject", sa.Uuid(), nullable=False),
        sa.Column("last_run_at", TS, nullable=True),
        sa.Column("next_run_at", TS, nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_schedules"),
    )
    op.create_index("ix_schedules_account_id", "schedules", ["account_id"])
    op.create_index(
        "ix_schedules_due", "schedules", ["next_run_at"], postgresql_where=sa.text("enabled")
    )
    op.create_table(
        "schedule_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("schedule_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("mower_id", sa.Uuid(), nullable=True),
        sa.Column("trigger", sa.String(16), nullable=False),
        sa.Column("scheduled_for", TS, nullable=False),
        sa.Column("started_at", TS, nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("details", sa.String(500), nullable=True),
        sa.Column("command_id", sa.Uuid(), nullable=True),
        sa.Column("issued_by", postgresql.JSONB(), nullable=True),
        sa.Column("window_end_at", TS, nullable=True),
        sa.Column("followup", sa.String(16), nullable=True),
        sa.Column("followup_at", TS, nullable=True),
        sa.ForeignKeyConstraint(
            ["schedule_id"],
            ["schedules.id"],
            name="fk_schedule_runs_schedule_id_schedules",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_schedule_runs"),
    )
    op.create_index(
        "ix_schedule_runs_schedule_started", "schedule_runs", ["schedule_id", "started_at"]
    )
    op.create_index("ix_schedule_runs_started_at", "schedule_runs", ["started_at"])
    op.create_index(
        "ix_schedule_runs_open",
        "schedule_runs",
        ["started_at"],
        postgresql_where=sa.text("outcome = 'started' AND followup IS NULL"),
    )
    op.create_table(
        "mower_status",
        sa.Column("mower_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("activity", sa.String(16), nullable=False),
        sa.Column("observed_at", TS, nullable=False),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("mower_id", name="pk_mower_status"),
    )
    op.create_table(
        "mower_places",
        sa.Column("mower_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("observed_at", TS, nullable=False),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("area_ids", postgresql.JSONB(), server_default=EMPTY_LIST, nullable=False),
        sa.PrimaryKeyConstraint("mower_id", name="pk_mower_places"),
    )


def downgrade() -> None:
    """Drop everything ``upgrade`` created."""
    op.drop_table("mower_places")
    op.drop_table("mower_status")
    op.drop_table("schedule_runs")
    op.drop_table("schedules")
