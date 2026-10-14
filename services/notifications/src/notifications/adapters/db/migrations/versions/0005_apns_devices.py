"""iOS devices for the ``apns`` channel (Apple Push Notification service).

A new table ``apns_devices`` (one row per device token, unique). Stored preferences get
``apns`` added to ``channels`` where it is missing, so a user who saved preferences before the
channel existed does not silently miss it (users without stored preferences get it from the
defaults). The downgrade drops the table and removes ``apns`` again.

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

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    """Add ``apns_devices`` and the channel ``apns`` to every stored preference."""
    op.create_table(
        "apns_devices",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("token", sa.String(200), nullable=False),
        sa.Column("device_name", sa.String(100), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_apns_devices"),
        sa.UniqueConstraint("token", name="uq_apns_devices_token"),
    )
    op.create_index("ix_apns_devices_user", "apns_devices", ["user_id", "account_id"])
    op.execute(
        sa.text(
            "UPDATE preferences SET channels = channels || '[\"apns\"]'::jsonb"
            " WHERE NOT channels @> '[\"apns\"]'::jsonb"
        )
    )


def downgrade() -> None:
    """Remove ``apns`` from every stored preference and drop ``apns_devices``."""
    op.execute(
        sa.text(
            "UPDATE preferences SET channels = channels - 'apns'"
            " WHERE channels @> '[\"apns\"]'::jsonb"
        )
    )
    op.drop_index("ix_apns_devices_user", table_name="apns_devices")
    op.drop_table("apns_devices")
