"""Credential fields of any cloud provider on linked accounts (ADR 0066).

Additive: ``linked_accounts.public_credentials`` (JSONB, the non-secret credential fields of the
provider's descriptor, identity included) and ``linked_accounts.secret_format`` (1: the sealed
value is one password, the form of ADR 0034; 2: a JSON object of every secret field). Existing
rows keep format 1 and read as an e-mail and a password; the next re-read with a key stores
them in format 2. ``email`` keeps its name and now holds the value of the provider's identity
field. A running older Fleet keeps working during the rollout: it neither reads nor writes the
new columns, and the rows it creates get their defaults.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``public_credentials`` and ``secret_format``."""
    op.add_column(
        "linked_accounts",
        sa.Column(
            "public_credentials",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "linked_accounts",
        sa.Column("secret_format", sa.SmallInteger(), nullable=False, server_default=sa.text("1")),
    )


def downgrade() -> None:
    """Drop both columns (rows in format 2 cannot be read by an older Fleet afterwards)."""
    op.drop_column("linked_accounts", "secret_format")
    op.drop_column("linked_accounts", "public_credentials")
