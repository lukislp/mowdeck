"""iOS widget credentials end with the login session that created them (ADR 0059).

``api_tokens.session_id``: the session family a widget credential was created in; once that
session has ended (logout, revoked in the security center) the credential no longer exchanges.
NULL for every other token and for widget credentials created before.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable column."""
    op.add_column("api_tokens", sa.Column("session_id", sa.Uuid(), nullable=True))


def downgrade() -> None:
    """Drop the column."""
    op.drop_column("api_tokens", "session_id")
