"""SQLAlchemy table definitions. Alembic compares migrations against :data:`metadata`.

Column names equal the field names of the rows in ``telemetry.domain.models``. Every table
carries ``account_id``, taken from the message or the mower directory, never from a request,
and every account-facing query filters by it.
"""

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Double,
    Float,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Table,
    Uuid,
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

mower_directory = Table(
    "mower_directory",
    metadata,
    Column("mower_id", Uuid, primary_key=True),
    Column("account_id", Uuid, nullable=False),
    Column("updated_at", TS, nullable=False),
    Index("ix_mower_directory_account", "account_id"),
)

mower_latest = Table(
    "mower_latest",
    metadata,
    Column("mower_id", Uuid, primary_key=True),
    Column("account_id", Uuid, nullable=False),
    Column("observed_at", TS, nullable=False),
    Column("received_at", TS, nullable=False),
    Column("activity", String(32), nullable=False),
    Column("battery_percent", SmallInteger, nullable=False),
    Column("state", JSONB, nullable=False),
    Column("history_written_at", TS),
    Column("history_fingerprint", String(256)),
    Column("position_fingerprint", String(128)),
    Index("ix_mower_latest_account", "account_id"),
)

mower_history = Table(
    "mower_history",
    metadata,
    Column("account_id", Uuid, nullable=False),
    Column("mower_id", Uuid, nullable=False),
    Column("observed_at", TS, nullable=False),
    Column("activity", String(32), nullable=False),
    Column("battery_percent", SmallInteger, nullable=False),
    Column("charging", Boolean, nullable=False),
    Column("rtk_fix", String(16), nullable=False),
    Column("connection", String(32), nullable=False),
    Column("blade_height_mm", SmallInteger),
    Column("speed_mps", Float),
    Column("error_code", String(64)),
    PrimaryKeyConstraint("mower_id", "observed_at", name="pk_mower_history"),
)
Index("ix_mower_history_mower_time", mower_history.c.mower_id, mower_history.c.observed_at.desc())
Index(
    "ix_mower_history_account_time", mower_history.c.account_id, mower_history.c.observed_at.desc()
)
Index("ix_mower_history_observed_brin", mower_history.c.observed_at, postgresql_using="brin")

mower_positions = Table(
    "mower_positions",
    metadata,
    Column("account_id", Uuid, nullable=False),
    Column("mower_id", Uuid, nullable=False),
    Column("observed_at", TS, nullable=False),
    Column("lat", Double, nullable=False),
    Column("lon", Double, nullable=False),
    Column("alt", Double),
    Column("heading", Float),
    Column("rtk_fix", String(16), nullable=False),
    Column("rtk_source", String(16), nullable=False),
    Column("wifi_rssi", SmallInteger),
    Column("mnet_rssi", SmallInteger),
    Column("blades_on", Boolean),
    PrimaryKeyConstraint("mower_id", "observed_at", name="pk_mower_positions"),
)
Index(
    "ix_mower_positions_mower_time",
    mower_positions.c.mower_id,
    mower_positions.c.observed_at.desc(),
)
Index(
    "ix_mower_positions_account_time",
    mower_positions.c.account_id,
    mower_positions.c.observed_at.desc(),
)
Index("ix_mower_positions_observed_brin", mower_positions.c.observed_at, postgresql_using="brin")

command_timeline = Table(
    "command_timeline",
    metadata,
    Column("account_id", Uuid, nullable=False),
    Column("mower_id", Uuid, nullable=False),
    Column("command_id", Uuid, nullable=False),
    Column("status", String(16), nullable=False),
    Column("reason", String(500)),
    Column("at", TS, nullable=False),
    PrimaryKeyConstraint("command_id", "status", name="pk_command_timeline"),
)
Index("ix_command_timeline_mower_time", command_timeline.c.mower_id, command_timeline.c.at.desc())
Index(
    "ix_command_timeline_account_time", command_timeline.c.account_id, command_timeline.c.at.desc()
)
Index("ix_command_timeline_at_brin", command_timeline.c.at, postgresql_using="brin")

mower_snapshots = Table(
    "mower_snapshots",
    metadata,
    Column("mower_id", Uuid, nullable=False),
    Column("kind", String(16), nullable=False),
    Column("account_id", Uuid, nullable=False),
    Column("observed_at", TS, nullable=False),
    Column("received_at", TS, nullable=False),
    Column("revision", String(128), nullable=False),
    Column("body", JSONB, nullable=False),
    PrimaryKeyConstraint("mower_id", "kind", name="pk_mower_snapshots"),
    Index("ix_mower_snapshots_account", "account_id"),
)

mower_health = Table(
    "mower_health",
    metadata,
    Column("mower_id", Uuid, primary_key=True),
    Column("account_id", Uuid, nullable=False),
    Column("since", TS),
    Column("blade_seconds", Double, nullable=False),
    Column("mowing_seconds", Double, nullable=False),
    Column("distance_m", Double, nullable=False),
    Column("battery_cycles", Integer, nullable=False),
    Column("charge_sessions", Integer, nullable=False),
    Column("errors", JSONB, nullable=False),
    Column("blade_seconds_since_change", Double, nullable=False),
    Column("blade_changed_at", TS),
    Column("blade_threshold_notified", Boolean, nullable=False),
    Column("last_observed_at", TS),
    Column("last_activity", String(32)),
    Column("last_charging", Boolean),
    Column("last_blade_on", Boolean),
    Column("last_lat", Double),
    Column("last_lon", Double),
    Column("last_error_code", String(64)),
    Column("battery_armed", Boolean, nullable=False),
    Column("updated_at", TS, nullable=False),
    Column("device_blade_seconds", Double),
    Column("device_blade_warn_seconds", Double),
    Column("device_battery_cycles", Integer),
    Column("blade_threshold_seconds", Double),
    Index("ix_mower_health_account", "account_id"),
)

mower_health_daily = Table(
    "mower_health_daily",
    metadata,
    Column("mower_id", Uuid, nullable=False),
    Column("account_id", Uuid, nullable=False),
    Column("day", Date, nullable=False),
    Column("blade_seconds", Double, nullable=False),
    Column("mowing_seconds", Double, nullable=False),
    Column("distance_m", Double, nullable=False),
    Column("battery_cycles", Integer, nullable=False),
    Column("charge_sessions", Integer, nullable=False),
    Column("errors", JSONB, nullable=False),
    Column("jobs", Integer, nullable=False, server_default="0"),
    Column("online_seconds", Double, nullable=False, server_default="0"),
    PrimaryKeyConstraint("mower_id", "day", name="pk_mower_health_daily"),
)

blade_changes = Table(
    "blade_changes",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("mower_id", Uuid, nullable=False),
    Column("account_id", Uuid, nullable=False),
    Column("changed_at", TS, nullable=False),
    Column("note", String(500)),
    Column("changed_by", Uuid),
)
Index("ix_blade_changes_mower_time", blade_changes.c.mower_id, blade_changes.c.changed_at.desc())
