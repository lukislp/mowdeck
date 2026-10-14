"""Entitlements schema: plans, account plans, the assignment history, the feature catalogue.

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
    """Create the tables and indexes."""
    op.create_table(
        "plans",
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "limits", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "features", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column("trial_days", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("file_digest", sa.String(64), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.CheckConstraint("trial_days >= 0", name=op.f("ck_plans_trial_days")),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_plans")),
    )
    op.create_table(
        "account_plans",
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("account_name", sa.String(200), nullable=True),
        sa.Column("plan_key", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("since", TS, nullable=False),
        sa.Column("trial_until", TS, nullable=True),
        sa.Column("grace_until", TS, nullable=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'trial', 'grace', 'expired')",
            name=op.f("ck_account_plans_status"),
        ),
        sa.CheckConstraint(
            "source IN ('default', 'manual', 'billing')", name=op.f("ck_account_plans_source")
        ),
        sa.ForeignKeyConstraint(
            ["plan_key"], ["plans.key"], name=op.f("fk_account_plans_plan_key_plans")
        ),
        sa.PrimaryKeyConstraint("account_id", name=op.f("pk_account_plans")),
    )
    op.create_index("ix_account_plans_since", "account_plans", ["since", "account_id"])
    op.create_table(
        "plan_history",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("at", TS, nullable=False),
        sa.Column("plan_key", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("trial_until", TS, nullable=True),
        sa.Column("grace_until", TS, nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("actor", sa.String(100), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plan_history")),
    )
    op.create_index("ix_plan_history_account_at", "plan_history", ["account_id", "at"])
    op.create_table(
        "features",
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("module", sa.String(64), nullable=False),
        sa.Column("first_seen_at", TS, nullable=False),
        sa.Column("last_seen_at", TS, nullable=False),
        sa.Column("retired", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_features")),
    )


def downgrade() -> None:
    """Drop everything."""
    op.drop_table("features")
    op.drop_index("ix_plan_history_account_at", table_name="plan_history")
    op.drop_table("plan_history")
    op.drop_index("ix_account_plans_since", table_name="account_plans")
    op.drop_table("account_plans")
    op.drop_table("plans")
