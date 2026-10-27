"""Camera photos of error and theft events (ADR 0056).

``notification_images`` holds one photo per event (JPEG bytes, a day at most, ``data`` NULL when
taking the photo failed) and ``inbox.image_id`` points the inbox items of that event at it. The
downgrade drops both; existing rows keep ``NULL`` and need no backfill.

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
    """Create ``notification_images`` and add ``inbox.image_id``."""
    op.create_table(
        "notification_images",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("mower_id", sa.Uuid(), nullable=False),
        sa.Column("media_type", sa.String(length=32), nullable=True),
        sa.Column("data", sa.LargeBinary(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_notification_images"),
    )
    op.create_index("ix_notification_images_expires", "notification_images", ["expires_at"])
    op.add_column("inbox", sa.Column("image_id", sa.Uuid(), nullable=True))


def downgrade() -> None:
    """Drop ``inbox.image_id`` and ``notification_images``."""
    op.drop_column("inbox", "image_id")
    op.drop_index("ix_notification_images_expires", table_name="notification_images")
    op.drop_table("notification_images")
