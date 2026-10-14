"""Notifications schema: mower directory and watch, preferences, targets, inbox, outbox.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    """Create the tables and their indexes."""
    op.create_table(
        "mower_directory",
        sa.Column("mower_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("mower_id", name="pk_mower_directory"),
    )
    op.create_index("ix_mower_directory_account", "mower_directory", ["account_id"])

    op.create_table(
        "mower_watch",
        sa.Column("mower_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("last_seen_at", TS, nullable=False),
        sa.Column("disconnected_at", TS, nullable=True),
        sa.Column("offline", sa.Boolean(), nullable=False),
        sa.Column("state", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("mower_id", name="pk_mower_watch"),
    )
    op.create_index(
        "ix_mower_watch_online_last_seen",
        "mower_watch",
        ["last_seen_at"],
        postgresql_where=sa.text("NOT offline"),
    )

    op.create_table(
        "preferences",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("enabled_kinds", postgresql.JSONB(), nullable=False),
        sa.Column("all_mowers", sa.Boolean(), nullable=False),
        sa.Column("mower_ids", postgresql.JSONB(), nullable=False),
        sa.Column("quiet_start", sa.String(5), nullable=True),
        sa.Column("quiet_end", sa.String(5), nullable=True),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("language", sa.String(8), nullable=False),
        sa.Column("channels", postgresql.JSONB(), nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("user_id", "account_id", name="pk_preferences"),
    )
    op.create_index("ix_preferences_account", "preferences", ["account_id"])

    op.create_table(
        "test_requests",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("last_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("user_id", "account_id", name="pk_test_requests"),
    )

    op.create_table(
        "push_subscriptions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("p256dh", sa.String(128), nullable=False),
        sa.Column("auth", sa.String(64), nullable=False),
        sa.Column("user_agent", sa.String(300), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_push_subscriptions"),
        sa.UniqueConstraint("endpoint", name="uq_push_subscriptions_endpoint"),
    )
    op.create_index("ix_push_subscriptions_user", "push_subscriptions", ["user_id", "account_id"])

    op.create_table(
        "ntfy_targets",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("server_url", sa.String(300), nullable=False),
        sa.Column("topic", sa.String(64), nullable=False),
        sa.Column("token", sa.String(200), nullable=True),
        sa.Column("updated_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("user_id", "account_id", name="pk_ntfy_targets"),
    )

    op.create_table(
        "inbox",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("mower_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("body", sa.String(1000), nullable=False),
        sa.Column("url", sa.String(500), nullable=True),
        sa.Column("dedupe_key", sa.String(200), nullable=False),
        sa.Column("occurred_at", TS, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("read_at", TS, nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_inbox"),
        sa.UniqueConstraint("user_id", "dedupe_key", name="uq_inbox_user_dedupe"),
    )
    op.create_index(
        "ix_inbox_user_time", "inbox", ["user_id", "account_id", sa.text("created_at DESC")]
    )

    op.create_table(
        "deliveries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("target_kind", sa.String(32), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.SmallInteger(), nullable=False),
        sa.Column("next_attempt_at", TS, nullable=False),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_deliveries"),
    )
    op.create_index(
        "ix_deliveries_due",
        "deliveries",
        ["next_attempt_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index("ix_deliveries_user", "deliveries", ["user_id", "account_id"])
    op.create_index("ix_deliveries_updated", "deliveries", ["updated_at"])


def downgrade() -> None:
    """Drop the tables (their indexes go with them)."""
    for table in (
        "deliveries",
        "inbox",
        "ntfy_targets",
        "push_subscriptions",
        "test_requests",
        "preferences",
        "mower_watch",
        "mower_directory",
    ):
        op.drop_table(table)
