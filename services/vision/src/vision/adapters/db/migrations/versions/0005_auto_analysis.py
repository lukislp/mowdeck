"""Automatic analyses on errors (ADR 0035): the trigger of a proposal, the users' opt-in (in
``user_preferences``, created by revision 0004) and the per-mower error watch.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``proposals.trigger`` (``manual`` for existing rows) and ``trigger_error_code``, the
    opt-in columns of ``user_preferences`` and the table ``auto_watch``."""
    op.add_column(
        "proposals",
        sa.Column("trigger", sa.String(16), server_default=sa.text("'manual'"), nullable=False),
    )
    op.add_column("proposals", sa.Column("trigger_error_code", sa.String(64), nullable=True))
    op.create_index(
        "ix_proposals_mower_trigger_created", "proposals", ["mower_id", "trigger", "created_at"]
    )
    op.add_column(
        "user_preferences",
        sa.Column("auto_analysis", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "user_preferences",
        sa.Column(
            "principal_kind", sa.String(16), server_default=sa.text("'user'"), nullable=False
        ),
    )
    op.create_index("ix_user_preferences_account", "user_preferences", ["account_id"])
    op.create_table(
        "auto_watch",
        sa.Column("mower_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("mower_id", name=op.f("pk_auto_watch")),
    )


def downgrade() -> None:
    """Drop what :func:`upgrade` added."""
    op.drop_table("auto_watch")
    op.drop_index("ix_user_preferences_account", table_name="user_preferences")
    op.drop_column("user_preferences", "principal_kind")
    op.drop_column("user_preferences", "auto_analysis")
    op.drop_index("ix_proposals_mower_trigger_created", table_name="proposals")
    op.drop_column("proposals", "trigger_error_code")
    op.drop_column("proposals", "trigger")
