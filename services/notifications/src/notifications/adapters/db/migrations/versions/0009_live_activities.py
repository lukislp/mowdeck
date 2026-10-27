"""Live Activities and widget reloads (ADR 0059).

``live_activity_devices`` holds one row per user and app installation: the ActivityKit
push-to-start token and whether the installation has widgets (those get a background push to
reload their timelines). ``live_activities`` holds the activities a job has on those devices:
the update token the app reported and what was last sent, so updates are rate limited and the
activity can be ended. Both are small and short lived. The downgrade drops both.

Revision ID: 0009
Revises: 0007
Create Date: 2026-10-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
# 0008 belongs to the deploy maintenance window PR (#167); re-pointed to it when it is merged.
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create both tables."""
    op.create_table(
        "live_activity_devices",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("installation_id", sa.Uuid(), nullable=False),
        sa.Column("push_to_start_token", sa.String(length=200), nullable=True),
        sa.Column("widgets_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "installation_id", name="pk_live_activity_devices"),
    )
    op.create_index("ix_live_activity_devices_account", "live_activity_devices", ["account_id"])
    op.create_table(
        "live_activities",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("installation_id", sa.Uuid(), nullable=False),
        sa.Column("mower_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activity_id", sa.String(length=100), nullable=True),
        sa.Column("update_token", sa.String(length=200), nullable=True),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_hash", sa.String(length=32), nullable=True),
        sa.PrimaryKeyConstraint(
            "user_id", "installation_id", "mower_id", name="pk_live_activities"
        ),
    )
    op.create_index("ix_live_activities_mower", "live_activities", ["mower_id"])


def downgrade() -> None:
    """Drop both tables."""
    op.drop_index("ix_live_activities_mower", table_name="live_activities")
    op.drop_table("live_activities")
    op.drop_index("ix_live_activity_devices_account", table_name="live_activity_devices")
    op.drop_table("live_activity_devices")
