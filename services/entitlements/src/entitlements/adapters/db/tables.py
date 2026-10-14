"""SQLAlchemy table definitions. Alembic compares migrations against :data:`metadata`.

* ``plans``: plans as data (limits as a JSON object, features as a JSON array).
* ``account_plans``: the plan of each account that has one (no row: the default plan).
* ``plan_history``: every assignment (the audit of manual changes).
* ``features``: the feature catalogue collected from the module descriptors.
"""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
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
STATUSES = "status IN ('active', 'trial', 'grace', 'expired')"
SOURCES = "source IN ('default', 'manual', 'billing')"

plans = Table(
    "plans",
    metadata,
    Column("key", String(64), primary_key=True),
    Column("name", String(200), nullable=False),
    Column("description", Text),
    Column("limits", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("features", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("trial_days", Integer, nullable=False, server_default=text("0")),
    Column("is_default", Boolean, nullable=False, server_default=text("false")),
    Column("active", Boolean, nullable=False, server_default=text("true")),
    Column("file_digest", String(64)),
    Column("created_at", TS, nullable=False),
    Column("updated_at", TS, nullable=False),
    CheckConstraint("trial_days >= 0", name="trial_days"),
)

account_plans = Table(
    "account_plans",
    metadata,
    Column("account_id", Uuid, primary_key=True),
    Column("account_name", String(200)),
    Column("plan_key", String(64), ForeignKey("plans.key"), nullable=False),
    Column("status", String(16), nullable=False),
    Column("since", TS, nullable=False),
    Column("trial_until", TS),
    Column("grace_until", TS),
    Column("source", String(16), nullable=False),
    Column("updated_at", TS, nullable=False),
    CheckConstraint(STATUSES, name="status"),
    CheckConstraint(SOURCES, name="source"),
)
Index("ix_account_plans_since", account_plans.c.since, account_plans.c.account_id)

plan_history = Table(
    "plan_history",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("account_id", Uuid, nullable=False),
    Column("at", TS, nullable=False),
    Column("plan_key", String(64), nullable=False),
    Column("status", String(16), nullable=False),
    Column("source", String(16), nullable=False),
    Column("trial_until", TS),
    Column("grace_until", TS),
    Column("reason", Text),
    Column("actor", String(100)),
)
Index("ix_plan_history_account_at", plan_history.c.account_id, plan_history.c.at)

features = Table(
    "features",
    metadata,
    Column("key", String(128), primary_key=True),
    Column("description", Text, nullable=False),
    Column("module", String(64), nullable=False),
    Column("first_seen_at", TS, nullable=False),
    Column("last_seen_at", TS, nullable=False),
    Column("retired", Boolean, nullable=False, server_default=text("false")),
)
