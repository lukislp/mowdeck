# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""The progress of removing an erased account's or a deleted user's stored events (ADR 0082).

``event_erasures``: one row while Identity reads the ``EVENTS`` stream for an account or a user
and erases what names them. The read is cut into slices; the row remembers where the stream
ended when the work began (``last_sequence``, so the work ends although events keep arriving),
which subject filter is being read and how far (``scanned_sequence``), so a restart continues
instead of starting over. The row is deleted when the work is done; it holds ids and counters,
nothing personal.

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the table."""
    op.create_table(
        "event_erasures",
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_sequence", sa.BigInteger(), nullable=False),
        sa.Column("filter_index", sa.Integer(), nullable=False),
        sa.Column("scanned_sequence", sa.BigInteger(), nullable=False),
        sa.Column("erased", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("subject_id", name=op.f("pk_event_erasures")),
    )


def downgrade() -> None:
    """Drop the table (work that was open starts over after the next upgrade)."""
    op.drop_table("event_erasures")
