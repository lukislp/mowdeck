"""Audit rows from events (ADR 0045): ``audit_log.event_id`` with a unique index.

Fleet writes the camera relay's stream starts and stops as audit rows from the ``CAMERA``
stream. A JetStream consumer may get an event more than once; the event id makes the write
idempotent (one row per event). Additive: one nullable column (every older row and every row
written from a request has none) and a unique index (NULLs never collide), so a running older
Fleet keeps working during the rollout. The append-only trigger is unaffected (it guards
``UPDATE`` and ``DELETE`` of rows, not the table's columns).

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``audit_log.event_id`` and its unique index."""
    op.add_column("audit_log", sa.Column("event_id", sa.Uuid(), nullable=True))
    op.create_index("uq_audit_log_event_id", "audit_log", ["event_id"], unique=True)


def downgrade() -> None:
    """Drop the index and the column (the event ids of earlier rows are lost)."""
    op.drop_index("uq_audit_log_event_id", table_name="audit_log")
    op.drop_column("audit_log", "event_id")
