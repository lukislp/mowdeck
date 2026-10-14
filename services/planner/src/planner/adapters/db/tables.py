"""SQLAlchemy table definitions. Alembic compares migrations against :data:`metadata`.

* ``mower_facts``: the newest map and capabilities snapshot of every mower (from the bus;
  Telemetry keeps the same, but services do not call each other).
* ``templates``: saved pattern chains per account (ADR 0030: the one piece of own data).
"""

from sqlalchemy import (
    Column,
    DateTime,
    Index,
    MetaData,
    String,
    Table,
    UniqueConstraint,
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

mower_facts = Table(
    "mower_facts",
    metadata,
    Column("mower_id", Uuid, primary_key=True),
    Column("account_id", Uuid, nullable=False),
    Column("map", JSONB),
    Column("map_observed_at", TS),
    Column("capabilities", JSONB),
    Column("capabilities_observed_at", TS),
)

templates = Table(
    "templates",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("account_id", Uuid, nullable=False),
    Column("name", String(100), nullable=False),
    Column("description", String(500)),
    Column("chain", JSONB, nullable=False),
    Column("created_by", Uuid, nullable=False),
    Column("created_at", TS, nullable=False),
    Column("updated_at", TS, nullable=False),
    UniqueConstraint("account_id", "name", name="uq_templates_account_name"),
)
Index("ix_templates_account", templates.c.account_id)
