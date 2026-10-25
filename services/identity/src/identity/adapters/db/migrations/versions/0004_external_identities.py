"""External identities (ADR 0051): provider accounts (GitHub) linked to users, and the single-use
OAuth round trips (state and PKCE challenge) of a sign-in or a link.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def _user_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["user_id"], ["users.id"], name=f"fk_{table}_user_id_users", ondelete="CASCADE"
    )


def upgrade() -> None:
    """Create the two tables."""
    op.create_table(
        "external_identities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=True),
        sa.Column("linked_at", TS, nullable=False),
        sa.Column("last_used_at", TS, nullable=True),
        _user_fk("external_identities"),
        sa.PrimaryKeyConstraint("id", name="pk_external_identities"),
        sa.UniqueConstraint("provider", "subject", name="uq_external_identities_provider_subject"),
        sa.UniqueConstraint("user_id", "provider", name="uq_external_identities_user_id_provider"),
    )
    op.create_table(
        "oauth_states",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("state_hash", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("purpose", sa.String(16), nullable=False),
        sa.Column("code_challenge", sa.String(64), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("return_query", sa.String(1024), nullable=True),
        _user_fk("oauth_states"),
        sa.PrimaryKeyConstraint("id", name="pk_oauth_states"),
        sa.UniqueConstraint("state_hash", name="uq_oauth_states_state_hash"),
    )
    op.create_index("ix_oauth_states_expires_at", "oauth_states", ["expires_at"])
    op.create_index("ix_oauth_states_user_id", "oauth_states", ["user_id"])


def downgrade() -> None:
    """Drop the tables (every GitHub link is lost; passwords and passkeys stay)."""
    for table in ("oauth_states", "external_identities"):
        op.drop_table(table)
