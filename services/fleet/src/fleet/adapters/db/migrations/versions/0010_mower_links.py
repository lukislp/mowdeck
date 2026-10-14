"""Additional links of a mower (ADR 0038): a Bluetooth bridge next to the cloud connection.

Additive: one new table, one row per mower and link kind, gone with the mower (``ON DELETE
CASCADE``). The link's secrets (the bridge's API encryption key) are stored AES-256-GCM encrypted
under ``MOWDECK_CREDENTIAL_ENCRYPTION_KEY`` like the passwords of linked accounts; ``proxy_info``
caches what the last scan learnt about the bridge. A running older Fleet keeps working during
the rollout (it neither reads nor writes the table).

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    """Create ``mower_links``."""
    op.create_table(
        "mower_links",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("mower_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column(
            "options",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("secrets_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("proxy_info", postgresql.JSONB(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_mower_links"),
        sa.ForeignKeyConstraint(
            ["mower_id"],
            ["mowers.id"],
            name="fk_mower_links_mower_id_mowers",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("mower_id", "kind", name="uq_mower_links_mower_kind"),
    )
    op.create_index("ix_mower_links_account_id", "mower_links", ["account_id"])


def downgrade() -> None:
    """Drop the table again (the links of every mower are lost: add them again afterwards)."""
    op.drop_index("ix_mower_links_account_id", table_name="mower_links")
    op.drop_table("mower_links")
