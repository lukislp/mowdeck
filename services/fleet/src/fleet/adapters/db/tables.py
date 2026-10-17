"""SQLAlchemy table definitions. Alembic compares migrations against :data:`metadata`.

Column names equal the field names of the entities in ``fleet.domain.models``. Every table
carries ``account_id`` and every account-facing query filters by it.
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    Uuid,
    func,
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

sites = Table(
    "sites",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("account_id", Uuid, nullable=False),
    Column("name", String(100), nullable=False),
    Column("timezone", String(64), nullable=False),
    Column("is_default", Boolean, nullable=False, server_default=text("false")),
    Column("address", JSONB),
    Column("created_by", Uuid),
    Column("created_at", TS, nullable=False),
    Column("updated_at", TS, nullable=False),
    # Optional WGS84 coordinates (weather for the Scheduler's rain pause): both or none.
    Column("latitude", Float),
    Column("longitude", Float),
    CheckConstraint(
        "(latitude IS NULL) = (longitude IS NULL)"
        " AND (latitude IS NULL OR latitude BETWEEN -90 AND 90)"
        " AND (longitude IS NULL OR longitude BETWEEN -180 AND 180)",
        name="coordinates",
    ),
    # The hidden default site: at most one per account.
    Index("uq_sites_one_default", "account_id", unique=True, postgresql_where=text("is_default")),
    # Named sites: unique per account, ignoring case.
    Index(
        "uq_sites_account_name",
        "account_id",
        func.lower(text("name")),
        unique=True,
        postgresql_where=text("NOT is_default"),
    ),
)

groups = Table(
    "groups",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("account_id", Uuid, nullable=False),
    Column("site_id", Uuid, ForeignKey("sites.id", ondelete="RESTRICT")),
    Column("name", String(100), nullable=False),
    Column("created_by", Uuid, nullable=False),
    Column("created_at", TS, nullable=False),
    Column("updated_at", TS, nullable=False),
    Index("uq_groups_account_name", "account_id", func.lower(text("name")), unique=True),
    Index("ix_groups_site_id", "site_id"),
)

# Linked vendor cloud accounts (ADR 0034); the password is AES-256-GCM encrypted.
linked_accounts = Table(
    "linked_accounts",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("account_id", Uuid, nullable=False),
    Column("provider", String(32), nullable=False),
    Column("email", String(254), nullable=False),
    Column("password_encrypted", LargeBinary, nullable=False),
    Column("devices", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("created_by", Uuid),
    Column("created_at", TS, nullable=False),
    Column("updated_at", TS, nullable=False),
    Column("last_synced_at", TS),
    Column("last_sync_error", String(500)),
    # One link per vendor account and MowDeck account, ignoring the case of the address.
    Index(
        "uq_linked_accounts_account_email",
        "account_id",
        "provider",
        func.lower(text("email")),
        unique=True,
    ),
)

mowers = Table(
    "mowers",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("account_id", Uuid, nullable=False),
    Column("site_id", Uuid, ForeignKey("sites.id", ondelete="RESTRICT"), nullable=False),
    Column("name", String(100), nullable=False),
    Column("model", String(100)),
    Column("serial", String(64)),
    Column("connection_kind", String(32), nullable=False),
    Column("connection_options", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_by", Uuid, nullable=False),
    Column("created_at", TS, nullable=False),
    Column("updated_at", TS, nullable=False),
    # Cloud mowers connect through a linked account (no own credentials, ADR 0034).
    Column("linked_account_id", Uuid, ForeignKey("linked_accounts.id", ondelete="RESTRICT")),
    # The vendor's device id: one physical mower in one MowDeck account only, across accounts.
    Column("iot_id", String(100)),
    # The mowing defaults of every client ({"pattern": MowPattern}); NULL until saved.
    Column("mow_defaults", JSONB),
    # Serial numbers are unique per account (several NULLs are allowed).
    UniqueConstraint("account_id", "serial", name="uq_mowers_account_serial"),
    Index("ix_mowers_account_site", "account_id", "site_id"),
    Index("ix_mowers_linked_account_id", "linked_account_id"),
    Index("uq_mowers_iot_id", "iot_id", unique=True),
)

mower_groups = Table(
    "mower_groups",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("mower_id", Uuid, ForeignKey("mowers.id", ondelete="CASCADE"), nullable=False),
    Column("group_id", Uuid, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False),
    Column("account_id", Uuid, nullable=False),
    UniqueConstraint("mower_id", "group_id", name="uq_mower_groups_pair"),
    Index("ix_mower_groups_group_id", "group_id"),
)

# Additional links of a mower next to its connection (ADR 0038): one row per mower and link
# kind, gone with the mower. The secrets are AES-256-GCM encrypted like linked account passwords.
mower_links = Table(
    "mower_links",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("mower_id", Uuid, ForeignKey("mowers.id", ondelete="CASCADE"), nullable=False),
    Column("account_id", Uuid, nullable=False),
    Column("kind", String(32), nullable=False),
    Column("options", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("secrets_encrypted", LargeBinary, nullable=False),
    # What the last scan learnt about the bridge (ProxyInfo as JSON); NULL until one answered.
    Column("proxy_info", JSONB),
    Column("created_by", Uuid),
    Column("created_at", TS, nullable=False),
    Column("updated_at", TS, nullable=False),
    UniqueConstraint("mower_id", "kind", name="uq_mower_links_mower_kind"),
    Index("ix_mower_links_account_id", "account_id"),
)

# Custom names of map areas per mower (the mower keeps its own names). Gone with the mower.
mower_area_names = Table(
    "mower_area_names",
    metadata,
    Column(
        "mower_id",
        Uuid,
        ForeignKey("mowers.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("area_id", String(128), primary_key=True),
    Column("account_id", Uuid, nullable=False),
    Column("name", String(60), nullable=False),
    Column("updated_at", TS, nullable=False),
    Column("updated_by", Uuid),
)

# The command audit and latest status. No foreign key to mowers: the record outlives the mower.
commands = Table(
    "commands",
    metadata,
    Column("command_id", Uuid, primary_key=True),
    Column("account_id", Uuid, nullable=False),
    Column("mower_id", Uuid, nullable=False),
    Column("bulk_id", Uuid),
    Column("kind", String(64), nullable=False),
    Column("command", JSONB, nullable=False),
    Column("priority", String(16), nullable=False),
    Column("issued_by_subject", Uuid, nullable=False),
    Column("issued_by", JSONB, nullable=False),
    Column("issued_at", TS, nullable=False),
    Column("expires_at", TS, nullable=False),
    Column("status", String(16), nullable=False),
    Column("reason", String(500)),
    Column("result_at", TS),
    Column("correlation_id", String(128), nullable=False),
    # Set when a service issued the command on behalf of a subject (fleet.commands.issue).
    Column("origin", String(32)),
    Column("schedule_id", Uuid),
    # Hash of the spot plan of a start_mow (the plan itself is not stored, ADR 0030).
    Column("plan_hash", String(64)),
    Index("ix_commands_account_mower_issued", "account_id", "mower_id", "issued_at"),
    Index("ix_commands_issued_at", "issued_at"),
)

# Manual drive sessions (ADR 0026): the audit of joystick driving. Frames are not stored.
drive_sessions = Table(
    "drive_sessions",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("account_id", Uuid, nullable=False),
    Column("mower_id", Uuid, nullable=False),
    Column("started_by_subject", Uuid, nullable=False),
    Column("started_by", JSONB, nullable=False),
    Column("started_at", TS, nullable=False),
    Column("expires_at", TS, nullable=False),
    Column("blade_on_allowed", Boolean, nullable=False, server_default=text("false")),
    Column("connection_id", Uuid),
    Column("ended_at", TS),
    Column("end_reason", String(32)),
    Column("frames", Integer),
    Column("frames_dropped", Integer),
    # Set when a service drove on behalf of the subject (Vision, ADR 0033); None: the joystick.
    Column("origin", String(32)),
    Index("ix_drive_sessions_account_mower_started", "account_id", "mower_id", "started_at"),
    Index("ix_drive_sessions_open", "mower_id", postgresql_where=text("ended_at IS NULL")),
)

# Append-only (a trigger rejects UPDATE and DELETE) record of site, group and mower changes.
audit_log = Table(
    "audit_log",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("at", TS, nullable=False),
    Column("action", String(64), nullable=False),
    Column("account_id", Uuid, nullable=False),
    Column("target_type", String(32), nullable=False),
    Column("target_id", String(64), nullable=False),
    Column("actor_subject", String(64)),
    Column("actor_kind", String(20)),
    Column("details", JSONB, nullable=False),
    Column("correlation_id", String(128)),
    # Rows written from events (camera relay, ADR 0045) carry the event id: one row per event.
    Column("event_id", Uuid),
    Index("ix_audit_log_account_id_id", "account_id", "id"),
    Index("uq_audit_log_event_id", "event_id", unique=True),
)
