"""The deploy maintenance window (ADR 0053).

``maintenance_window`` holds at most one row (``id = 1``): when the deploy opened the window,
when it expires by itself and when it was closed. Notifications reads it to hold back the
``mower_offline`` / ``mower_online`` pair that a restart of the Device-Gateway causes.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``maintenance_window``."""
    op.create_table(
        "maintenance_window",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.String(length=100), nullable=False),
        sa.CheckConstraint("id = 1", name=op.f("ck_maintenance_window_single_row")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_maintenance_window")),
    )


def downgrade() -> None:
    """Drop ``maintenance_window``."""
    op.drop_table("maintenance_window")
