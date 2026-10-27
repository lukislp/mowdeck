"""Theft tracking mode (ADR 0053): sessions, share links, photos; name and serial in the
mower directory.

Additive only: two nullable columns and three new tables.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    """Add the directory columns and the three tracking tables."""
    op.add_column("mower_directory", sa.Column("name", sa.String(100), nullable=True))
    op.add_column("mower_directory", sa.Column("serial", sa.String(100), nullable=True))

    op.create_table(
        "tracking_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("mower_id", sa.Uuid(), nullable=False),
        sa.Column("trigger", sa.String(40), nullable=False),
        sa.Column("started_at", TS, nullable=False),
        sa.Column("ends_at", TS, nullable=False),
        sa.Column("interval_seconds", sa.Double(), nullable=False),
        sa.Column("mower_name", sa.String(100), nullable=False),
        sa.Column("mower_model", sa.String(64), nullable=True),
        sa.Column("mower_serial", sa.String(100), nullable=True),
        sa.Column("started_by", sa.Uuid(), nullable=True),
        sa.Column("ended_at", TS, nullable=True),
        sa.Column("end_reason", sa.String(24), nullable=True),
        sa.Column("last_lat", sa.Double(), nullable=True),
        sa.Column("last_lon", sa.Double(), nullable=True),
        sa.Column("last_fix_at", TS, nullable=True),
        sa.Column("stale_since", TS, nullable=True),
        sa.Column("stale_notified", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("notice_lat", sa.Double(), nullable=True),
        sa.Column("notice_lon", sa.Double(), nullable=True),
        sa.Column("notice_at", TS, nullable=True),
        sa.Column("points", sa.Integer(), server_default="0", nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_tracking_sessions"),
    )
    op.create_index(
        "ix_tracking_sessions_mower_started", "tracking_sessions", ["mower_id", "started_at"]
    )
    op.create_index("ix_tracking_sessions_account", "tracking_sessions", ["account_id"])
    op.create_index(
        "uq_tracking_sessions_active",
        "tracking_sessions",
        ["mower_id"],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
    )

    op.create_table(
        "tracking_links",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("mower_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("revoked_at", TS, nullable=True),
        sa.Column("last_used_at", TS, nullable=True),
        sa.Column("uses", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["tracking_sessions.id"],
            name="fk_tracking_links_session_id_tracking_sessions",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tracking_links"),
    )
    op.create_index("uq_tracking_links_token_hash", "tracking_links", ["token_hash"], unique=True)
    op.create_index("ix_tracking_links_session", "tracking_links", ["session_id"])

    op.create_table(
        "tracking_photos",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("taken_at", TS, nullable=False),
        sa.Column("media_type", sa.String(16), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["tracking_sessions.id"],
            name="fk_tracking_photos_session_id_tracking_sessions",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tracking_photos"),
    )
    op.create_index("ix_tracking_photos_session", "tracking_photos", ["session_id", "taken_at"])


def downgrade() -> None:
    """Drop what :func:`upgrade` added."""
    op.drop_table("tracking_photos")
    op.drop_table("tracking_links")
    op.drop_table("tracking_sessions")
    op.drop_column("mower_directory", "serial")
    op.drop_column("mower_directory", "name")
