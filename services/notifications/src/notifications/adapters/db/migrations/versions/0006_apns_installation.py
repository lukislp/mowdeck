"""One APNs registration per app installation.

``apns_devices`` gets a nullable ``installation_id`` (a UUID the iOS shell keeps in the
Keychain) and a unique constraint on ``(user_id, installation_id)``: a new token of the same
installation replaces the old row instead of adding one, so a device that got a new token
(reinstall, TestFlight over a development build, token rotation) no longer receives every
notification twice. Existing rows keep ``NULL`` (PostgreSQL treats NULLs as distinct), so older
app builds that do not send the id keep the one-row-per-token behaviour. The downgrade drops the
constraint and the column.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``installation_id`` and the unique constraint per user and installation."""
    op.add_column("apns_devices", sa.Column("installation_id", sa.Uuid(), nullable=True))
    op.create_unique_constraint(
        "uq_apns_devices_user_installation", "apns_devices", ["user_id", "installation_id"]
    )


def downgrade() -> None:
    """Drop the constraint and ``installation_id``."""
    op.drop_constraint("uq_apns_devices_user_installation", "apns_devices", type_="unique")
    op.drop_column("apns_devices", "installation_id")
