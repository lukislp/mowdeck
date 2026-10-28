"""Setup tour (ADR 0062): a user's own choices in the web app's onboarding tour.

One row per user: whether the tour runs, the steps they skipped and the "first time here" hints
they dismissed. Existing users get no row (status ``new``); the web app then offers at most a
dismissible hint to accounts that already have a mower.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the table."""
    op.create_table(
        "onboarding_progress",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("skipped_steps", postgresql.JSONB(), nullable=False),
        sa.Column("dismissed_hints", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_onboarding_progress_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", name="pk_onboarding_progress"),
    )


def downgrade() -> None:
    """Drop the table."""
    op.drop_table("onboarding_progress")
