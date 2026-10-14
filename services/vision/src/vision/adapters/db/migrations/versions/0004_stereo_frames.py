"""Stereo frames: proposals remember what each image was and whether it was a stereo pair;
users keep their AI assist preferences.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``proposals.stereo`` and ``proposals.frames`` and the ``user_preferences`` table."""
    op.add_column(
        "proposals",
        sa.Column("stereo", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "proposals",
        sa.Column(
            "frames",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.create_table(
        "user_preferences",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("stereo_frames", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "account_id"),
    )


def downgrade() -> None:
    """Drop the preferences table and the two proposal columns."""
    op.drop_table("user_preferences")
    op.drop_column("proposals", "frames")
    op.drop_column("proposals", "stereo")
