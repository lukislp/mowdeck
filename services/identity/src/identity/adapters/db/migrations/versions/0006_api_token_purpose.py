"""Personal access tokens (ADR 0064): the purpose of an API token.

``integration`` for every existing token (created by an account admin, or an iOS widget
credential), ``personal`` for the tokens users create for their own scripts.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the column; existing tokens are integrations."""
    op.add_column(
        "api_tokens",
        sa.Column("purpose", sa.String(16), nullable=False, server_default="integration"),
    )


def downgrade() -> None:
    """Drop the column."""
    op.drop_column("api_tokens", "purpose")
