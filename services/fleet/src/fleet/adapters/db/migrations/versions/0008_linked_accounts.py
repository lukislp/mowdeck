"""Linked vendor cloud accounts and the mowers that connect through them (ADR 0034).

Additive: a new table and two nullable columns (``linked_account_id`` with its foreign key,
``iot_id`` with an installation-wide unique index: one physical mower in one MowDeck account),
so a running older Fleet keeps working during the rollout. Existing cloud mowers are not moved:
they keep their inline credentials until they are deleted and added again from a linked
account (see the README's upgrade notes).

Follows 0007 (area names, merged before this change); on a branch without 0007 Alembic cannot
resolve the chain until it is rebased onto main.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    """Create ``linked_accounts`` and add ``mowers.linked_account_id``."""
    op.create_table(
        "linked_accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("password_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column(
            "devices",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("last_synced_at", TS, nullable=True),
        sa.Column("last_sync_error", sa.String(500), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_linked_accounts"),
    )
    op.create_index(
        "uq_linked_accounts_account_email",
        "linked_accounts",
        ["account_id", "provider", sa.text("lower(email)")],
        unique=True,
    )
    op.add_column("mowers", sa.Column("linked_account_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_mowers_linked_account_id_linked_accounts",
        "mowers",
        "linked_accounts",
        ["linked_account_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_mowers_linked_account_id", "mowers", ["linked_account_id"])
    # One physical mower (vendor IoT id) in one MowDeck account only, installation-wide.
    op.add_column("mowers", sa.Column("iot_id", sa.String(100), nullable=True))
    op.create_index("uq_mowers_iot_id", "mowers", ["iot_id"], unique=True)


def downgrade() -> None:
    """Drop the column and the table again.

    Cloud mowers that were moved to a linked account lose their credentials: re-register them
    (or restore a backup) after a downgrade.
    """
    op.drop_index("uq_mowers_iot_id", table_name="mowers")
    op.drop_column("mowers", "iot_id")
    op.drop_index("ix_mowers_linked_account_id", table_name="mowers")
    op.drop_constraint("fk_mowers_linked_account_id_linked_accounts", "mowers", type_="foreignkey")
    op.drop_column("mowers", "linked_account_id")
    op.drop_index("uq_linked_accounts_account_email", table_name="linked_accounts")
    op.drop_table("linked_accounts")
