"""Theft alarm: geofences per mower, acknowledged inbox items and alarm silences.

Additive only: a nullable column on ``inbox`` and two new tables.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    """Add ``inbox.acked_at``, ``mower_geofence`` and ``alarm_silences``."""
    op.add_column("inbox", sa.Column("acked_at", TS, nullable=True))

    op.create_table(
        "mower_geofence",
        sa.Column("mower_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("observed_at", TS, nullable=False),
        sa.Column("fence", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("mower_id", name="pk_mower_geofence"),
    )

    op.create_table(
        "alarm_silences",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("silence_key", sa.String(100), nullable=False),
        sa.Column("until", TS, nullable=False),
        sa.PrimaryKeyConstraint("user_id", "account_id", "silence_key", name="pk_alarm_silences"),
    )


def downgrade() -> None:
    """Drop what :func:`upgrade` added."""
    op.drop_table("alarm_silences")
    op.drop_table("mower_geofence")
    op.drop_column("inbox", "acked_at")
