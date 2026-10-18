"""Return to dock proposals: what approval executes (``drive`` steps or ``return_to_dock``) and
whether it is the rule based dock fallback after the model recommended nothing.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``proposals.action`` (``drive`` for existing rows) and ``proposals.fallback``."""
    op.add_column(
        "proposals",
        sa.Column("action", sa.String(16), server_default=sa.text("'drive'"), nullable=False),
    )
    op.add_column(
        "proposals",
        sa.Column("fallback", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )


def downgrade() -> None:
    """Drop what :func:`upgrade` added."""
    op.drop_column("proposals", "fallback")
    op.drop_column("proposals", "action")
