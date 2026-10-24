"""A recommendation below the confidence threshold is offered next to the fallback (ADR 0033
addendum 2026-10-24): ``low_confidence``, the alternative action, why the recommendation was
not offered, and which option the approval chose.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``low_confidence`` (false for existing rows), ``alternative_action``,
    ``model_refused`` and ``approved_option``."""
    op.add_column(
        "proposals",
        sa.Column("low_confidence", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("proposals", sa.Column("alternative_action", sa.String(16), nullable=True))
    op.add_column("proposals", sa.Column("model_refused", sa.String(300), nullable=True))
    op.add_column("proposals", sa.Column("approved_option", sa.String(16), nullable=True))


def downgrade() -> None:
    """Drop what :func:`upgrade` added."""
    op.drop_column("proposals", "approved_option")
    op.drop_column("proposals", "model_refused")
    op.drop_column("proposals", "alternative_action")
    op.drop_column("proposals", "low_confidence")
