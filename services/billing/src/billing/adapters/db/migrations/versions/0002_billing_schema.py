"""Billing schema: accounts, customers, subscriptions, invoices, webhook events.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    """Create the tables and indexes."""
    op.create_table(
        "accounts",
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=True),
        sa.Column("kind", sa.String(16), nullable=True),
        sa.Column("created_at", TS, nullable=True),
        sa.PrimaryKeyConstraint("account_id", name=op.f("pk_accounts")),
    )
    op.create_table(
        "customers",
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("provider_customer_id", sa.String(255), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("account_id", "provider", name=op.f("pk_customers")),
        sa.UniqueConstraint("provider", "provider_customer_id", name=op.f("uq_customers_provider")),
    )
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("provider_subscription_id", sa.String(255), nullable=False),
        sa.Column("plan_key", sa.String(64), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("current_period_end", TS, nullable=True),
        sa.Column("trial_end", TS, nullable=True),
        sa.Column(
            "cancel_at_period_end", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("plan_status", sa.String(16), nullable=True),
        sa.Column("grace_until", TS, nullable=True),
        sa.Column("last_event_at", TS, nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_subscriptions")),
        sa.UniqueConstraint(
            "provider", "provider_subscription_id", name=op.f("uq_subscriptions_provider")
        ),
    )
    op.create_index("ix_subscriptions_account", "subscriptions", ["account_id", "created_at"])
    op.create_table(
        "invoices",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("provider_invoice_id", sa.String(255), nullable=False),
        sa.Column("provider_subscription_id", sa.String(255), nullable=True),
        sa.Column("number", sa.String(100), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("amount_due", sa.BigInteger(), nullable=False),
        sa.Column("amount_paid", sa.BigInteger(), nullable=False),
        sa.Column("hosted_url", sa.Text(), nullable=True),
        sa.Column("pdf_url", sa.Text(), nullable=True),
        sa.Column("issued_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invoices")),
        sa.UniqueConstraint("provider", "provider_invoice_id", name=op.f("uq_invoices_provider")),
    )
    op.create_index("ix_invoices_account_issued", "invoices", ["account_id", "issued_at"])
    op.create_table(
        "webhook_events",
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("event_id", sa.String(255), nullable=False),
        sa.Column("type", sa.String(100), nullable=False),
        sa.Column("received_at", TS, nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("claimed_at", TS, nullable=True),
        sa.Column("processed_at", TS, nullable=True),
        sa.Column("outcome", sa.String(32), nullable=True),
        sa.Column("account_id", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("provider", "event_id", name=op.f("pk_webhook_events")),
    )


def downgrade() -> None:
    """Drop the tables."""
    op.drop_table("webhook_events")
    op.drop_index("ix_invoices_account_issued", table_name="invoices")
    op.drop_table("invoices")
    op.drop_index("ix_subscriptions_account", table_name="subscriptions")
    op.drop_table("subscriptions")
    op.drop_table("customers")
    op.drop_table("accounts")
