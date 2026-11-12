# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Manual refunds: refunds of erased accounts the operator pays back by hand (ADR 0082).

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    """Create the table and its index."""
    op.create_table(
        "manual_refunds",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(600), nullable=False),
        sa.Column("payment_ref", sa.String(255), nullable=False),
        sa.Column("invoice_ref", sa.String(255), nullable=True),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("settled_at", TS, nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_manual_refunds")),
        sa.UniqueConstraint("key", name=op.f("uq_manual_refunds_key")),
    )
    op.create_index("ix_manual_refunds_settled", "manual_refunds", ["settled_at"], unique=False)


def downgrade() -> None:
    """Drop the table."""
    op.drop_index("ix_manual_refunds_settled", table_name="manual_refunds")
    op.drop_table("manual_refunds")
