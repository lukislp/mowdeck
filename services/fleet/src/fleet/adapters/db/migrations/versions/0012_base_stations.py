"""RTK base stations and the mowers that use them (ADR 0047).

Additive: a new table ``base_stations`` (an installation-wide unique index on ``iot_id``: one
physical base station in one MowDeck account) and the nullable column ``mowers.base_station_id``
(foreign key, ``ON DELETE SET NULL``), so a running older Fleet keeps working during the rollout
(it neither reads nor writes either). Nothing is migrated: base stations are registered from the
device list of a linked account after the upgrade.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    """Create ``base_stations`` and add ``mowers.base_station_id``."""
    op.create_table(
        "base_stations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("linked_account_id", sa.Uuid(), nullable=True),
        sa.Column("device_name", sa.String(100), nullable=True),
        sa.Column("iot_id", sa.String(100), nullable=True),
        sa.Column("model", sa.String(100), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_base_stations"),
        sa.ForeignKeyConstraint(
            ["linked_account_id"],
            ["linked_accounts.id"],
            name="fk_base_stations_linked_account_id_linked_accounts",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("source IN ('cloud', 'simulated')", name="ck_base_stations_source"),
    )
    op.create_index("ix_base_stations_account_id", "base_stations", ["account_id"])
    op.create_index("ix_base_stations_linked_account_id", "base_stations", ["linked_account_id"])
    op.create_index("uq_base_stations_iot_id", "base_stations", ["iot_id"], unique=True)
    op.add_column("mowers", sa.Column("base_station_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_mowers_base_station_id_base_stations",
        "mowers",
        "base_stations",
        ["base_station_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_mowers_base_station_id", "mowers", ["base_station_id"])


def downgrade() -> None:
    """Drop the column and the table again (the base stations and their links are lost:
    register them again afterwards)."""
    op.drop_index("ix_mowers_base_station_id", table_name="mowers")
    op.drop_constraint("fk_mowers_base_station_id_base_stations", "mowers", type_="foreignkey")
    op.drop_column("mowers", "base_station_id")
    op.drop_index("uq_base_stations_iot_id", table_name="base_stations")
    op.drop_index("ix_base_stations_linked_account_id", table_name="base_stations")
    op.drop_index("ix_base_stations_account_id", table_name="base_stations")
    op.drop_table("base_stations")
