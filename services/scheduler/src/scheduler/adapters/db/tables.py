"""SQLAlchemy table definitions. Alembic compares migrations against :data:`metadata`.

Column names equal the field names of the entities in ``scheduler.domain.models``. Schedules and
runs carry ``account_id``; every account-facing query filters by it.
"""

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Time,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData(
    naming_convention={
        "ix": "ix_%(column_0_label)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)
"""Metadata of all tables of the service."""

TS = DateTime(timezone=True)

schedules = Table(
    "schedules",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("account_id", Uuid, nullable=False),
    Column("name", String(100), nullable=False),
    Column("target_type", String(16), nullable=False),
    Column("target_id", Uuid, nullable=False),
    Column("kind", String(16), nullable=False),
    Column("days_of_week", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("anchor_date", Date),
    Column("interval_days", Integer),
    Column("start_time", Time, nullable=False),
    Column("duration_minutes", Integer, nullable=False),
    Column("timezone", String(64), nullable=False),
    Column("timezone_override", String(64)),
    Column("area_ids", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("pattern", JSONB),
    Column("rotation_angles", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("rotation_index", Integer, nullable=False, server_default=text("0")),
    Column("rain", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("pause_running_jobs_on_rain", Boolean, nullable=False, server_default=text("true")),
    Column("return_to_dock_at_window_end", Boolean, nullable=False, server_default=text("false")),
    Column("enabled", Boolean, nullable=False, server_default=text("true")),
    Column("disabled_reason", String(500)),
    Column("permission_skips", Integer, nullable=False, server_default=text("0")),
    Column("skip_until", TS),
    Column("owner", JSONB, nullable=False),
    Column("owner_subject", Uuid, nullable=False),
    Column("last_run_at", TS),
    Column("next_run_at", TS),
    Column("created_at", TS, nullable=False),
    Column("updated_at", TS, nullable=False),
    Index("ix_schedules_account_id", "account_id"),
    # The ticker's question: enabled schedules that are due.
    Index("ix_schedules_due", "next_run_at", postgresql_where=text("enabled")),
)

schedule_runs = Table(
    "schedule_runs",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("schedule_id", Uuid, ForeignKey("schedules.id", ondelete="CASCADE"), nullable=False),
    Column("account_id", Uuid, nullable=False),
    Column("mower_id", Uuid),
    Column("trigger", String(16), nullable=False),
    Column("scheduled_for", TS, nullable=False),
    Column("started_at", TS, nullable=False),
    Column("outcome", String(32), nullable=False),
    Column("details", String(500)),
    Column("command_id", Uuid),
    Column("issued_by", JSONB),
    Column("window_end_at", TS),
    Column("followup", String(16)),
    Column("followup_at", TS),
    Index("ix_schedule_runs_schedule_started", "schedule_id", "started_at"),
    Index("ix_schedule_runs_started_at", "started_at"),
    # Started runs still followed (rain pause of running jobs, window end).
    Index(
        "ix_schedule_runs_open",
        "started_at",
        postgresql_where=text("outcome = 'started' AND followup IS NULL"),
    ),
)

# Latest activity per mower (telemetry.mower.*), for the busy check and the last position.
mower_status = Table(
    "mower_status",
    metadata,
    Column("mower_id", Uuid, primary_key=True),
    Column("account_id", Uuid, nullable=False),
    Column("activity", String(16), nullable=False),
    Column("observed_at", TS, nullable=False),
    Column("latitude", Float),
    Column("longitude", Float),
)

# Map facts per mower (device.mower.<id>.map): geo origin and area ids.
mower_places = Table(
    "mower_places",
    metadata,
    Column("mower_id", Uuid, primary_key=True),
    Column("account_id", Uuid, nullable=False),
    Column("observed_at", TS, nullable=False),
    Column("latitude", Float),
    Column("longitude", Float),
    Column("area_ids", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
)
