"""Planner schema: mower facts (map and capability snapshots) and pattern templates.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    """Create ``mower_facts`` and ``templates``."""
    op.create_table(
        "mower_facts",
        sa.Column("mower_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("map", postgresql.JSONB(), nullable=True),
        sa.Column("map_observed_at", TS, nullable=True),
        sa.Column("capabilities", postgresql.JSONB(), nullable=True),
        sa.Column("capabilities_observed_at", TS, nullable=True),
        sa.PrimaryKeyConstraint("mower_id", name="pk_mower_facts"),
    )
    op.create_table(
        "templates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.String(500), nullable=True),
        sa.Column("chain", postgresql.JSONB(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_templates"),
        sa.UniqueConstraint("account_id", "name", name="uq_templates_account_name"),
    )
    op.create_index("ix_templates_account", "templates", ["account_id"])


def downgrade() -> None:
    """Drop both tables."""
    op.drop_index("ix_templates_account", table_name="templates")
    op.drop_table("templates")
    op.drop_table("mower_facts")
