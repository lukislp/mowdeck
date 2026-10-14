"""Fleet schema: sites (one hidden default site per account), groups, mowers, group
memberships, command records and the append-only audit log.

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
    """Create the tables, indexes and the append-only trigger of the audit log."""
    op.create_table(
        "sites",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("address", postgresql.JSONB(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_sites"),
    )
    op.create_index(
        "uq_sites_one_default",
        "sites",
        ["account_id"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )
    op.create_index(
        "uq_sites_account_name",
        "sites",
        ["account_id", sa.text("lower(name)")],
        unique=True,
        postgresql_where=sa.text("NOT is_default"),
    )
    op.create_table(
        "groups",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("site_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.ForeignKeyConstraint(
            ["site_id"], ["sites.id"], name="fk_groups_site_id_sites", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_groups"),
    )
    op.create_index(
        "uq_groups_account_name",
        "groups",
        ["account_id", sa.text("lower(name)")],
        unique=True,
    )
    op.create_index("ix_groups_site_id", "groups", ["site_id"])
    op.create_table(
        "mowers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("site_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("model", sa.String(100), nullable=True),
        sa.Column("serial", sa.String(64), nullable=True),
        sa.Column("connection_kind", sa.String(32), nullable=False),
        sa.Column(
            "connection_options",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.ForeignKeyConstraint(
            ["site_id"], ["sites.id"], name="fk_mowers_site_id_sites", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_mowers"),
        sa.UniqueConstraint("account_id", "serial", name="uq_mowers_account_serial"),
    )
    op.create_index("ix_mowers_account_site", "mowers", ["account_id", "site_id"])
    op.create_table(
        "mower_groups",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("mower_id", sa.Uuid(), nullable=False),
        sa.Column("group_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["mower_id"], ["mowers.id"], name="fk_mower_groups_mower_id_mowers", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["group_id"], ["groups.id"], name="fk_mower_groups_group_id_groups", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_mower_groups"),
        sa.UniqueConstraint("mower_id", "group_id", name="uq_mower_groups_pair"),
    )
    op.create_index("ix_mower_groups_group_id", "mower_groups", ["group_id"])
    op.create_table(
        "commands",
        sa.Column("command_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("mower_id", sa.Uuid(), nullable=False),
        sa.Column("bulk_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("command", postgresql.JSONB(), nullable=False),
        sa.Column("priority", sa.String(16), nullable=False),
        sa.Column("issued_by_subject", sa.Uuid(), nullable=False),
        sa.Column("issued_by", postgresql.JSONB(), nullable=False),
        sa.Column("issued_at", TS, nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(500), nullable=True),
        sa.Column("result_at", TS, nullable=True),
        sa.Column("correlation_id", sa.String(128), nullable=False),
        sa.PrimaryKeyConstraint("command_id", name="pk_commands"),
    )
    op.create_index(
        "ix_commands_account_mower_issued", "commands", ["account_id", "mower_id", "issued_at"]
    )
    op.create_index("ix_commands_issued_at", "commands", ["issued_at"])
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("at", TS, nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("target_type", sa.String(32), nullable=False),
        sa.Column("target_id", sa.String(64), nullable=False),
        sa.Column("actor_subject", sa.String(64), nullable=True),
        sa.Column("actor_kind", sa.String(20), nullable=True),
        sa.Column("details", postgresql.JSONB(), nullable=False),
        sa.Column("correlation_id", sa.String(128), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_audit_log"),
    )
    op.create_index("ix_audit_log_account_id_id", "audit_log", ["account_id", "id"])
    op.execute(
        """
        CREATE FUNCTION fleet_audit_log_append_only() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_log is append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_log_append_only
        BEFORE UPDATE OR DELETE ON audit_log
        FOR EACH ROW EXECUTE FUNCTION fleet_audit_log_append_only()
        """
    )


def downgrade() -> None:
    """Drop everything ``upgrade`` created."""
    op.execute("DROP TRIGGER IF EXISTS audit_log_append_only ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS fleet_audit_log_append_only()")
    op.drop_table("audit_log")
    op.drop_table("commands")
    op.drop_table("mower_groups")
    op.drop_table("mowers")
    op.drop_table("groups")
    op.drop_table("sites")
