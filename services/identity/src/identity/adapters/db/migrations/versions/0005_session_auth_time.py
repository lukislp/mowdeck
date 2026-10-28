"""Step-up authentication (ADR 0053): when and how the user last authenticated interactively,
kept on the login session and copied on every refresh rotation.

Existing sessions get NULL: they count as stale and the first critical action asks to confirm.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the two columns."""
    op.add_column("sessions", sa.Column("auth_time", sa.DateTime(timezone=True), nullable=True))
    op.add_column("sessions", sa.Column("auth_method", sa.String(20), nullable=True))


def downgrade() -> None:
    """Drop the two columns."""
    op.drop_column("sessions", "auth_method")
    op.drop_column("sessions", "auth_time")
