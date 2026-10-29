"""Security center (ADR 0065): known devices, password status, new-login alerts.

* ``known_devices``: the browsers and app installations a user signed in from (the SHA-256 of
  the random id in the ``mowdeck_device`` cookie, a label, first and last seen). Existing users
  start without rows; their next login records the device silently.
* ``users.password_changed_at``: when the password was last set (unknown, NULL, for existing
  users) and ``users.new_login_alerts`` (on for everyone).
* Two indexes on ``audit_log`` for a user's own security events across accounts.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the table, add the columns and the indexes."""
    op.create_table(
        "known_devices",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("label", sa.String(100), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_ip_prefix", sa.String(64), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_known_devices_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_known_devices"),
        sa.UniqueConstraint("user_id", "token_hash", name="uq_known_devices_user_id_token_hash"),
    )
    op.add_column("users", sa.Column("password_changed_at", sa.DateTime(timezone=True)))
    op.add_column(
        "users",
        sa.Column("new_login_alerts", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.create_index("ix_audit_log_target_id_at", "audit_log", ["target_id", "at"])
    op.create_index("ix_audit_log_actor_subject_at", "audit_log", ["actor_subject", "at"])


def downgrade() -> None:
    """Drop the indexes, the columns and the table."""
    op.drop_index("ix_audit_log_actor_subject_at", table_name="audit_log")
    op.drop_index("ix_audit_log_target_id_at", table_name="audit_log")
    op.drop_column("users", "new_login_alerts")
    op.drop_column("users", "password_changed_at")
    op.drop_table("known_devices")
