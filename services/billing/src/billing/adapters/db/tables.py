# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""SQLAlchemy table definitions. Alembic compares migrations against :data:`metadata`.

* ``accounts``: name and kind of the accounts (from ``identity.account.created``).
* ``customers``: the provider's customer of each account (no card data, only the id).
* ``subscriptions``: the subscriptions as the provider's webhooks reported them.
* ``invoices``: invoice references (amounts, status, links to the hosted page and the PDF).
* ``webhook_events``: every provider event id once (exactly-once processing).
* ``manual_refunds``: refunds of erased accounts the operator pays back by hand (ADR 0082). The
  one table without an ``account_id``: it names the payment and the invoice at the provider,
  the amount and why, and nobody (kept under Art. 17(3)(b) GDPR until the repayment is done,
  deleted 30 days after it is settled).
* ``payment_facts``: the payments ledger of the operator dashboard (ADR 0083): anonymous facts
  (paid invoices, failed payments, refunds, disputes, started and ended subscriptions) with
  amount, currency, plan and time. No ``account_id``, no customer, no provider object id: the
  ``ref`` is a keyed hash of the object id for deduplication and status updates, cleared 180
  days after the fact last changed. Never deleted, so the account erasure does not touch it.
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)

metadata = MetaData(
    naming_convention={
        "ix": "ix_%(column_0_label)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)
"""Metadata of all tables of the service."""

TS = DateTime(timezone=True)

accounts = Table(
    "accounts",
    metadata,
    Column("account_id", Uuid, primary_key=True),
    Column("name", String(200)),
    Column("kind", String(16)),
    Column("created_at", TS),
)

customers = Table(
    "customers",
    metadata,
    Column("account_id", Uuid, nullable=False),
    Column("provider", String(32), nullable=False),
    Column("provider_customer_id", String(255), nullable=False),
    Column("created_at", TS, nullable=False),
    PrimaryKeyConstraint("account_id", "provider"),
    UniqueConstraint("provider", "provider_customer_id"),
)

subscriptions = Table(
    "subscriptions",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("account_id", Uuid, nullable=False),
    Column("provider", String(32), nullable=False),
    Column("provider_subscription_id", String(255), nullable=False),
    Column("plan_key", String(64)),
    Column("status", String(32), nullable=False),
    Column("current_period_end", TS),
    Column("trial_end", TS),
    Column("cancel_at_period_end", Boolean, nullable=False, server_default=text("false")),
    Column("plan_status", String(16)),
    Column("grace_until", TS),
    Column("last_event_at", TS),
    Column("created_at", TS, nullable=False),
    Column("updated_at", TS, nullable=False),
    UniqueConstraint("provider", "provider_subscription_id"),
)
Index("ix_subscriptions_account", subscriptions.c.account_id, subscriptions.c.created_at)

invoices = Table(
    "invoices",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("account_id", Uuid, nullable=False),
    Column("provider", String(32), nullable=False),
    Column("provider_invoice_id", String(255), nullable=False),
    Column("provider_subscription_id", String(255)),
    Column("number", String(100)),
    Column("status", String(32), nullable=False),
    Column("currency", String(3), nullable=False),
    Column("amount_due", BigInteger, nullable=False),
    Column("amount_paid", BigInteger, nullable=False),
    Column("hosted_url", Text),
    Column("pdf_url", Text),
    Column("issued_at", TS, nullable=False),
    Column("updated_at", TS, nullable=False),
    UniqueConstraint("provider", "provider_invoice_id"),
)
Index("ix_invoices_account_issued", invoices.c.account_id, invoices.c.issued_at)

webhook_events = Table(
    "webhook_events",
    metadata,
    Column("provider", String(32), nullable=False),
    Column("event_id", String(255), nullable=False),
    Column("type", String(100), nullable=False),
    Column("received_at", TS, nullable=False),
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    Column("claimed_at", TS),
    Column("processed_at", TS),
    Column("outcome", String(32)),
    Column("account_id", Uuid),
    PrimaryKeyConstraint("provider", "event_id"),
)

manual_refunds = Table(
    "manual_refunds",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("key", String(600), nullable=False),
    Column("payment_ref", String(255), nullable=False),
    Column("invoice_ref", String(255)),
    Column("amount", BigInteger, nullable=False),
    Column("currency", String(3), nullable=False),
    Column("reason", Text, nullable=False),
    Column("created_at", TS, nullable=False),
    Column("settled_at", TS),
    UniqueConstraint("key"),
)
Index("ix_manual_refunds_settled", manual_refunds.c.settled_at)

payment_facts = Table(
    "payment_facts",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("kind", String(32), nullable=False),
    Column("ref", String(64)),
    Column("provider", String(32), nullable=False),
    Column("occurred_at", TS, nullable=False),
    Column("currency", String(3)),
    Column("amount", BigInteger, nullable=False, server_default=text("0")),
    Column("tax", BigInteger),
    Column("plan_key", String(64)),
    Column("status", String(32)),
    Column("reason", String(64)),
    Column("period_start", TS),
    Column("period_end", TS),
    Column("attempt", Integer),
    Column("closed_at", TS),
    Column("due_by", TS),
    Column("event_at", TS, nullable=False),
    Column("recorded_at", TS, nullable=False),
    Column("updated_at", TS, nullable=False),
    # NULLs are distinct: facts whose hash was cleared never collide.
    UniqueConstraint("kind", "ref", name="uq_payment_facts_kind_ref"),
)
Index("ix_payment_facts_kind_occurred", payment_facts.c.kind, payment_facts.c.occurred_at)
Index("ix_payment_facts_updated", payment_facts.c.updated_at)
