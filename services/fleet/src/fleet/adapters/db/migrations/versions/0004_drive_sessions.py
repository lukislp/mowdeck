"""Manual drive sessions (joystick over Realtime, ADR 0026).

Additive: a new table only, so a running older Fleet keeps working during the rollout.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    """Create ``drive_sessions``."""
    op.create_table(
        "drive_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("mower_id", sa.Uuid(), nullable=False),
        sa.Column("started_by_subject", sa.Uuid(), nullable=False),
        sa.Column("started_by", postgresql.JSONB(), nullable=False),
        sa.Column("started_at", TS, nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column(
            "blade_on_allowed", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("connection_id", sa.Uuid(), nullable=True),
        sa.Column("ended_at", TS, nullable=True),
        sa.Column("end_reason", sa.String(32), nullable=True),
        sa.Column("frames", sa.Integer(), nullable=True),
        sa.Column("frames_dropped", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_drive_sessions"),
    )
    op.create_index(
        "ix_drive_sessions_account_mower_started",
        "drive_sessions",
        ["account_id", "mower_id", "started_at"],
    )
    op.create_index(
        "ix_drive_sessions_open",
        "drive_sessions",
        ["mower_id"],
        postgresql_where=sa.text("ended_at IS NULL"),
    )


def downgrade() -> None:
    """Drop ``drive_sessions``."""
    op.drop_table("drive_sessions")
