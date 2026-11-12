# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Payment facts: the anonymous payments ledger of the operator dashboard (ADR 0083).

The statistics start with this migration: nothing is backfilled from the provider.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    """Create the table and its indexes."""
    op.create_table(
        "payment_facts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("ref", sa.String(64), nullable=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("occurred_at", TS, nullable=False),
        sa.Column("currency", sa.String(3), nullable=True),
        sa.Column("amount", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("tax", sa.BigInteger(), nullable=True),
        sa.Column("plan_key", sa.String(64), nullable=True),
        sa.Column("status", sa.String(32), nullable=True),
        sa.Column("reason", sa.String(64), nullable=True),
        sa.Column("period_start", TS, nullable=True),
        sa.Column("period_end", TS, nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=True),
        sa.Column("closed_at", TS, nullable=True),
        sa.Column("due_by", TS, nullable=True),
        sa.Column("event_at", TS, nullable=False),
        sa.Column("recorded_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payment_facts")),
        sa.UniqueConstraint("kind", "ref", name="uq_payment_facts_kind_ref"),
    )
    op.create_index(
        "ix_payment_facts_kind_occurred",
        "payment_facts",
        ["kind", "occurred_at"],
        unique=False,
    )
    op.create_index("ix_payment_facts_updated", "payment_facts", ["updated_at"], unique=False)


def downgrade() -> None:
    """Drop the table."""
    op.drop_index("ix_payment_facts_updated", table_name="payment_facts")
    op.drop_index("ix_payment_facts_kind_occurred", table_name="payment_facts")
    op.drop_table("payment_facts")
