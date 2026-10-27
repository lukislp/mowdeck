"""SQLAlchemy table definitions. Alembic compares migrations against :data:`metadata`.

Every user-facing table carries ``user_id`` and ``account_id``, both taken from the verified
token (API) or from the message (consumers), and every user-facing query filters by both.
"""

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Index,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Table,
    Text,
    UniqueConstraint,
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

mower_directory = Table(
    "mower_directory",
    metadata,
    Column("mower_id", Uuid, primary_key=True),
    Column("account_id", Uuid, nullable=False),
    Column("name", String(100), nullable=False),
    Column("updated_at", TS, nullable=False),
    Index("ix_mower_directory_account", "account_id"),
)

mower_watch = Table(
    "mower_watch",
    metadata,
    Column("mower_id", Uuid, primary_key=True),
    Column("account_id", Uuid, nullable=False),
    Column("last_seen_at", TS, nullable=False),
    Column("disconnected_at", TS),
    Column("offline", Boolean, nullable=False),
    Column("state", JSONB, nullable=False),
    Index(
        "ix_mower_watch_online_last_seen",
        "last_seen_at",
        postgresql_where=text("NOT offline"),
    ),
)

preferences = Table(
    "preferences",
    metadata,
    Column("user_id", Uuid, nullable=False),
    Column("account_id", Uuid, nullable=False),
    Column("enabled_kinds", JSONB, nullable=False),
    Column("all_mowers", Boolean, nullable=False),
    Column("mower_ids", JSONB, nullable=False),
    Column("quiet_start", String(5)),
    Column("quiet_end", String(5)),
    Column("timezone", String(64), nullable=False),
    Column("language", String(8), nullable=False),
    Column("channels", JSONB, nullable=False),
    Column("updated_at", TS, nullable=False),
    PrimaryKeyConstraint("user_id", "account_id", name="pk_preferences"),
    Index("ix_preferences_account", "account_id"),
)

test_requests = Table(
    "test_requests",
    metadata,
    Column("user_id", Uuid, nullable=False),
    Column("account_id", Uuid, nullable=False),
    Column("last_at", TS, nullable=False),
    PrimaryKeyConstraint("user_id", "account_id", name="pk_test_requests"),
)

push_subscriptions = Table(
    "push_subscriptions",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("user_id", Uuid, nullable=False),
    Column("account_id", Uuid, nullable=False),
    Column("endpoint", Text, nullable=False),
    Column("p256dh", String(128), nullable=False),
    Column("auth", String(64), nullable=False),
    Column("user_agent", String(300)),
    Column("created_at", TS, nullable=False),
    UniqueConstraint("endpoint", name="uq_push_subscriptions_endpoint"),
    Index("ix_push_subscriptions_user", "user_id", "account_id"),
)

apns_devices = Table(
    "apns_devices",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("user_id", Uuid, nullable=False),
    Column("account_id", Uuid, nullable=False),
    Column("token", String(200), nullable=False),
    Column("device_name", String(100)),
    Column("created_at", TS, nullable=False),
    Column("installation_id", Uuid),
    UniqueConstraint("token", name="uq_apns_devices_token"),
    UniqueConstraint("user_id", "installation_id", name="uq_apns_devices_user_installation"),
    Index("ix_apns_devices_user", "user_id", "account_id"),
)

ntfy_targets = Table(
    "ntfy_targets",
    metadata,
    Column("user_id", Uuid, nullable=False),
    Column("account_id", Uuid, nullable=False),
    Column("server_url", String(300), nullable=False),
    Column("topic", String(64), nullable=False),
    Column("token", String(200)),
    Column("updated_at", TS, nullable=False),
    PrimaryKeyConstraint("user_id", "account_id", name="pk_ntfy_targets"),
)

inbox = Table(
    "inbox",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("user_id", Uuid, nullable=False),
    Column("account_id", Uuid, nullable=False),
    Column("mower_id", Uuid),
    Column("kind", String(32), nullable=False),
    Column("severity", String(16), nullable=False),
    Column("title", String(200), nullable=False),
    Column("body", String(1000), nullable=False),
    Column("url", String(500)),
    Column("dedupe_key", String(200), nullable=False),
    Column("occurred_at", TS, nullable=False),
    Column("created_at", TS, nullable=False),
    Column("read_at", TS),
    Column("acked_at", TS),
    Column("image_id", Uuid),
    UniqueConstraint("user_id", "dedupe_key", name="uq_inbox_user_dedupe"),
)

notification_images = Table(
    "notification_images",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("account_id", Uuid, nullable=False),
    Column("mower_id", Uuid, nullable=False),
    Column("media_type", String(32)),
    Column("data", LargeBinary),
    Column("created_at", TS, nullable=False),
    Column("expires_at", TS, nullable=False),
    Index("ix_notification_images_expires", "expires_at"),
)
"""The camera photo of an error or theft event (ADR 0056): one row per event, shared by the
inbox items that reference it (``inbox.image_id``). ``data`` is NULL for a failed capture."""
Index("ix_inbox_user_time", inbox.c.user_id, inbox.c.account_id, inbox.c.created_at.desc())

deliveries = Table(
    "deliveries",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("user_id", Uuid, nullable=False),
    Column("account_id", Uuid, nullable=False),
    Column("channel", String(32), nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("target_kind", String(32), nullable=False),
    Column("target_id", Uuid),
    Column("status", String(16), nullable=False),
    Column("attempts", SmallInteger, nullable=False),
    Column("next_attempt_at", TS, nullable=False),
    Column("last_error", String(500)),
    Column("created_at", TS, nullable=False),
    Column("updated_at", TS, nullable=False),
    Index(
        "ix_deliveries_due",
        "next_attempt_at",
        postgresql_where=text("status = 'pending'"),
    ),
    Index("ix_deliveries_user", "user_id", "account_id"),
    Index("ix_deliveries_updated", "updated_at"),
)

mower_geofence = Table(
    "mower_geofence",
    metadata,
    Column("mower_id", Uuid, primary_key=True),
    Column("account_id", Uuid, nullable=False),
    Column("observed_at", TS, nullable=False),
    Column("fence", JSONB, nullable=False),
)

alarm_silences = Table(
    "alarm_silences",
    metadata,
    Column("user_id", Uuid, nullable=False),
    Column("account_id", Uuid, nullable=False),
    Column("silence_key", String(100), nullable=False),
    Column("until", TS, nullable=False),
    PrimaryKeyConstraint("user_id", "account_id", "silence_key", name="pk_alarm_silences"),
)
