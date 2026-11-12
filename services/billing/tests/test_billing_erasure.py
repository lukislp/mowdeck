# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Account erasure (ADR 0082): ``account.erase.billing`` ends the subscriptions and deletes the
customer at the payment provider, then removes everything Billing holds of the account, and
reports honestly when it could not. Provider events that come afterwards change nothing.

The requests go over the in-memory bus to the real app, as Identity sends them, with the fake
payment provider (no network, no keys). The store is the memory store and, with
``MOWDECK_TEST_DATABASE_URL``, PostgreSQL (see ``test_billing_db``).
"""

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from billing_support import Harness, harness, make_settings
from mowdeck_contracts.v1 import (
    BILLING_SUBSCRIPTION_CHANGED,
    AccountCreated,
    AccountEraseReply,
    AccountEraseRequest,
    AccountKind,
    PlanSetReply,
    PlanSetRequest,
    SubscriptionStatus,
)
from mowdeck_contracts.v1.subjects import account_erase_subject, event_subject_for_type
from mowdeck_service_kit.db import account_tables
from mowdeck_service_kit.erasure import ErasureIncomplete
from test_billing_db import STORES, store_of

from billing.adapters.db.tables import manual_refunds, metadata, webhook_events
from billing.adapters.memory import MemoryTx
from billing.adapters.providers.fake import FakeProvider, fake_customer_id
from billing.adapters.providers.fixtures import (
    checkout_completed_event,
    customer_event,
    invoice_event,
    subscription_event,
)
from billing.application.ports import Store
from billing.domain.erasure import LOCAL_PROVIDERS, unreachable_providers
from billing.domain.errors import ProviderError, ProviderUnavailable
from billing.domain.models import Customer, ProviderSubscription, Subscription
from billing.main import ensure_identity_asks

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
DAY = 86400
ACCOUNT_TABLES = {table.name for table in account_tables(metadata)}
"""Every table that can hold rows of an account, read from the schema."""


async def erase(h: Harness, account_id: UUID) -> AccountEraseReply:
    """What Identity's ``BusAccountEraser`` sends, and the decoded reply."""
    request = AccountEraseRequest(account_id=account_id, requested_at=NOW)
    message = await h.bus.request(account_erase_subject("billing"), request, timeout=5.0)
    return message.model(AccountEraseReply)


async def rows_of(store: Store, account_id: UUID) -> dict[str, int]:
    """The rows of the account per table, as the port reports them."""
    async with store.transaction() as tx:
        return await tx.account_rows(account_id)


def _subscription_event(
    h: Harness, account_id: UUID, subscription_id: str, status: str, kind: str = "updated"
) -> dict[str, Any]:
    """A ``customer.subscription.*`` event as Stripe sends it for a subscription that Billing's
    checkout created: the metadata names the account and the plan."""
    now = h.clock.ts
    return subscription_event(
        f"customer.subscription.{kind}",
        subscription_id=subscription_id,
        customer_id=fake_customer_id(account_id),
        status=status,
        price_id="price_home",
        account_id=account_id,
        plan="home",
        period_start=now - DAY,
        period_end=now + 29 * DAY,
        ended_at=now if status == "canceled" else None,
        created=now,
    )


def _invoice_event(
    h: Harness, account_id: UUID, subscription_id: str, kind: str = "invoice.paid"
) -> dict[str, Any]:
    return invoice_event(
        kind,
        invoice_id=f"in_{subscription_id}_{kind[-4:]}",
        customer_id=fake_customer_id(account_id),
        subscription_id=subscription_id,
        status="paid" if kind == "invoice.paid" else "open",
        amount=900,
        created=h.clock.ts,
    )


async def subscribe(h: Harness, name: str, subscription_id: str) -> UUID:
    """An account as a paying customer leaves it, through the real paths: Identity announced
    it, the owner started a checkout (the customer at the provider), the provider reported the
    subscription and a paid invoice."""
    account_id = uuid4()
    await h.service.account_created(
        AccountCreated(
            account_id=account_id, name=name, kind=AccountKind.HOUSEHOLD, owner_user_id=uuid4()
        )
    )
    await h.service.start_checkout(account_id, "home", "https://app.example.org")
    customer_id = fake_customer_id(account_id)
    h.provider.subscriptions[subscription_id] = ProviderSubscription(
        id=subscription_id, customer_id=customer_id, status=SubscriptionStatus.ACTIVE
    )
    created = _subscription_event(h, account_id, subscription_id, "active", "created")
    assert (await h.webhook(created)).json()["outcome"] == "applied"
    paid = _invoice_event(h, account_id, subscription_id)
    assert (await h.webhook(paid)).json()["outcome"] == "applied"
    return account_id


def _changes(h: Harness) -> int:
    return len(h.bus.messages(event_subject_for_type(BILLING_SUBSCRIPTION_CHANGED)))


# --- the schema ------------------------------------------------------------------------------


def test_every_table_of_billing_belongs_to_an_account() -> None:
    """The erasure counts the tables with an ``account_id`` column. A table without one would
    be invisible to it: this fails when one is added, so its rows get an owner or a decision.
    No table is keyed by a provider id alone (a customer, subscription or session id).

    The one decision so far: ``manual_refunds`` (refunds the operator pays back by hand) has no
    account on purpose. It must outlive the erasure until the money went back (Art. 17(3)(b)
    GDPR) and names only the payment and the invoice at the provider, never the account, a
    name or an e-mail address (its columns are pinned below).

    The second: ``payment_facts`` (the payments ledger, ADR 0083) holds anonymous statistics
    that outlive the erasure on purpose; its columns are pinned in
    ``test_billing_ledger.py``."""
    assert {table.name for table in metadata.sorted_tables} - ACCOUNT_TABLES == {
        "manual_refunds",
        "payment_facts",
    }
    assert {column.name for column in manual_refunds.columns} == {
        "id",
        "key",
        "payment_ref",
        "invoice_ref",
        "amount",
        "currency",
        "reason",
        "created_at",
        "settled_at",
    }
    assert {
        "accounts",
        "customers",
        "subscriptions",
        "invoices",
        "webhook_events",
    } == ACCOUNT_TABLES


def test_the_event_ledger_stores_no_payload() -> None:
    """A provider event carries an e-mail address and a name; the ledger keeps the event id, its
    type, times, the outcome and the account. A row of an event that belongs to no account
    (an unknown customer, an ignored type) therefore names nobody and may stay after an
    erasure. Whoever adds a column here decides again."""
    assert {column.name for column in webhook_events.columns} == {
        "provider",
        "event_id",
        "type",
        "received_at",
        "attempts",
        "claimed_at",
        "processed_at",
        "outcome",
        "account_id",
    }


def test_only_the_fake_provider_is_deleted_without_asking() -> None:
    assert frozenset({"fake"}) == LOCAL_PROVIDERS
    assert unreachable_providers(["stripe", "fake", "stripe"], "stripe") == []
    assert unreachable_providers(["stripe", "paddle"], "fake") == ["paddle", "stripe"]
    assert unreachable_providers([], "stripe") == []


# --- the request -----------------------------------------------------------------------------


@pytest.mark.parametrize("kind", STORES)
async def test_erasure_ends_the_subscription_deletes_the_customer_and_every_row(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with store_of(kind, monkeypatch) as store, harness(store=store) as h:  # type: ignore[arg-type]
        gone = await subscribe(h, "Garden", "sub_gone")
        kept = await subscribe(h, "Other garden", "sub_kept")
        before = await rows_of(store, gone)
        kept_rows = await rows_of(store, kept)
        # The seed reaches every table the schema gives an account, so none is checked empty.
        assert set(before) == ACCOUNT_TABLES
        plans_set = len(h.entitlements.requests)
        published = _changes(h)

        first = await erase(h, gone)
        calls = list(h.provider.calls)
        second = await erase(h, gone)
        unknown = await erase(h, uuid4())

        assert first == AccountEraseReply(ok=True, deleted=sum(before.values()))
        assert second == AccountEraseReply(ok=True, deleted=0)
        assert unknown == AccountEraseReply(ok=True, deleted=0)
        # At the provider: each subscription and the customer exactly once, in this order,
        # and nothing again for an account whose rows are gone.
        assert calls == [
            ("cancel_subscription", "sub_gone"),
            ("delete_customer", fake_customer_id(gone)),
        ]
        assert h.provider.calls == calls
        assert h.provider.cancelled == ["sub_gone"]
        assert h.provider.deleted == [fake_customer_id(gone)]
        assert fake_customer_id(gone) not in h.provider.customers
        # The other account is untouched, here and at the provider.
        assert await rows_of(store, gone) == {}
        assert await rows_of(store, kept) == kept_rows
        assert h.provider.subscriptions["sub_kept"].status is SubscriptionStatus.ACTIVE
        assert fake_customer_id(kept) in h.provider.customers
        # The erasure itself sets no plan and publishes nothing: Entitlements deletes the
        # account's plan when Identity asks it, after Billing.
        assert len(h.entitlements.requests) == plans_set
        assert _changes(h) == published
        assert (await h.service.summary(gone)).subscription is None
        assert await h.service.invoices(gone) == []


async def test_only_open_subscriptions_are_ended() -> None:
    async with harness() as h:
        account_id = await subscribe(h, "Garden", "sub_old")
        ended = _subscription_event(h, account_id, "sub_old", "canceled", "deleted")
        assert (await h.webhook(ended)).json()["outcome"] == "applied"
        h.provider.subscriptions["sub_new"] = ProviderSubscription(
            id="sub_new",
            customer_id=fake_customer_id(account_id),
            status=SubscriptionStatus.PAST_DUE,
        )
        h.clock.advance(minutes=1)
        current = _subscription_event(h, account_id, "sub_new", "past_due", "created")
        assert (await h.webhook(current)).json()["outcome"] == "applied"

        assert (await erase(h, account_id)).ok

        assert h.provider.calls == [
            ("cancel_subscription", "sub_new"),
            ("delete_customer", fake_customer_id(account_id)),
        ]


async def test_an_account_without_a_customer_only_loses_its_rows() -> None:
    """Most accounts never buy anything: Billing knows their name and nothing else."""
    async with harness() as h:
        account_id = uuid4()
        await h.service.account_created(
            AccountCreated(
                account_id=account_id,
                name="Garden",
                kind=AccountKind.HOUSEHOLD,
                owner_user_id=uuid4(),
            )
        )

        reply = await erase(h, account_id)

        assert reply == AccountEraseReply(ok=True, deleted=1)
        assert h.provider.calls == []
        assert await rows_of(h.store, account_id) == {}


# --- the provider fails ------------------------------------------------------------------------


@pytest.mark.parametrize("kind", STORES)
async def test_a_provider_failure_fails_the_erasure_keeps_the_rows_and_a_retry_finishes(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Billing never confirms while the customer may still exist at the provider. The rows
    stay, because they are what names the customer for the next attempt."""
    async with store_of(kind, monkeypatch) as store, harness(store=store) as h:  # type: ignore[arg-type]
        account_id = await subscribe(h, "Garden", "sub_1")
        customer_id = fake_customer_id(account_id)
        before = await rows_of(store, account_id)

        h.provider.failures["delete_customer"] = ProviderUnavailable("rate limited")
        failed = await erase(h, account_id)
        left = await rows_of(store, account_id)
        del h.provider.failures["delete_customer"]
        retried = await erase(h, account_id)

        assert failed == AccountEraseReply(
            ok=False, deleted=0, error="ProviderUnavailable: see the log of billing"
        )
        assert left == before
        assert retried == AccountEraseReply(ok=True, deleted=sum(before.values()))
        assert await rows_of(store, account_id) == {}
        # The subscription was over after the first attempt; asking again changed nothing.
        assert h.provider.calls == [
            ("cancel_subscription", "sub_1"),
            ("delete_customer", customer_id),
            ("cancel_subscription", "sub_1"),
            ("delete_customer", customer_id),
        ]
        assert h.provider.cancelled == ["sub_1"]
        assert h.provider.deleted == [customer_id]


async def test_a_failing_cancellation_stops_before_the_customer() -> None:
    async with harness() as h:
        account_id = await subscribe(h, "Garden", "sub_1")
        before = await rows_of(h.store, account_id)
        h.provider.failures["cancel_subscription"] = ProviderError("refused")

        failed = await erase(h, account_id)

        assert failed == AccountEraseReply(
            ok=False, deleted=0, error="ProviderError: see the log of billing"
        )
        assert h.provider.calls == [("cancel_subscription", "sub_1")]
        assert h.provider.deleted == []
        assert await rows_of(h.store, account_id) == before


async def test_a_customer_at_another_provider_is_never_confirmed_away() -> None:
    """An installation that changed its provider cannot delete the old one's customer: the
    erasure fails instead of leaving a paying customer behind."""
    async with harness() as h:  # the configured provider is the fake
        account_id = uuid4()
        async with h.store.transaction() as tx:
            await tx.save_customer(Customer(account_id, "stripe", "cus_live", NOW))

        failed = await erase(h, account_id)

        assert failed == AccountEraseReply(
            ok=False, deleted=0, error="ProviderError: see the log of billing"
        )
        assert h.provider.calls == []
        assert await rows_of(h.store, account_id) == {"customers": 1}


async def test_rows_of_the_fake_provider_go_without_a_call() -> None:
    """An installation that tried Billing with the fake provider and then went live: the fake
    kept nothing anywhere, so its rows are deleted and the real provider is not asked."""
    live = FakeProvider(name="stripe")
    async with harness(provider=live) as h:
        account_id = uuid4()
        async with h.store.transaction() as tx:
            await tx.save_customer(Customer(account_id, "fake", "cus_fake_1", NOW))
            await tx.save_subscription(
                Subscription(
                    id=uuid4(),
                    account_id=account_id,
                    provider="fake",
                    provider_subscription_id="sub_fake_1",
                    plan_key="home",
                    status=SubscriptionStatus.ACTIVE,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )

        reply = await erase(h, account_id)

        assert reply == AccountEraseReply(ok=True, deleted=2)
        assert live.calls == []


async def test_erasure_reports_rows_that_remain_and_a_retry_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A purge that leaves rows behind is never answered as a success."""

    async def purge_nothing(self: MemoryTx, account_id: UUID) -> int:
        del self, account_id
        return 0

    async with harness() as h:
        account_id = await subscribe(h, "Garden", "sub_1")
        before = await rows_of(h.store, account_id)
        with monkeypatch.context() as patched:
            patched.setattr(MemoryTx, "purge_account", purge_nothing)
            failed = await erase(h, account_id)
        retried = await erase(h, account_id)

        assert failed == AccountEraseReply(
            ok=False, deleted=0, error=str(ErasureIncomplete(before))
        )
        assert retried == AccountEraseReply(ok=True, deleted=sum(before.values()))
        assert await rows_of(h.store, account_id) == {}


# --- provider events after the erasure ---------------------------------------------------------


@pytest.mark.parametrize("kind", STORES)
async def test_late_provider_events_bring_nothing_back(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the provider sends after the customer is gone, in its real shapes: the cancellation
    of the subscription and the deleted customer (both caused by the erasure itself), an
    invoice, a checkout that completed, and a delivery of an event from before the erasure
    that the provider repeats. The subscription events name the account in their metadata; that
    is not enough. Each is acknowledged, none writes a row, sets a plan or is published."""
    async with store_of(kind, monkeypatch) as store, harness(store=store) as h:  # type: ignore[arg-type]
        account_id = await subscribe(h, "Garden", "sub_1")
        customer_id = fake_customer_id(account_id)
        earlier = _subscription_event(h, account_id, "sub_1", "active")
        assert (await h.webhook(earlier)).json()["outcome"] == "applied"
        assert (await erase(h, account_id)).ok
        plans_set = len(h.entitlements.requests)
        published = _changes(h)
        h.clock.advance(seconds=5)

        late = [
            _subscription_event(h, account_id, "sub_1", "canceled", "deleted"),
            _subscription_event(h, account_id, "sub_1", "active"),
            {
                **_subscription_event(h, account_id, "sub_1", "trialing"),
                "type": "customer.subscription.trial_will_end",
            },
            _invoice_event(h, account_id, "sub_1", "invoice.paid"),
            _invoice_event(h, account_id, "sub_1", "invoice.payment_failed"),
            checkout_completed_event(
                session_id="cs_late",
                customer_id=customer_id,
                subscription_id="sub_1",
                account_id=account_id,
                plan="home",
            ),
            checkout_completed_event(
                session_id="cs_bare",
                customer_id=customer_id,
                subscription_id=None,
                account_id=account_id,
                plan="home",
            ),
            earlier,  # the same event id again: its ledger row went with the account
        ]
        outcomes = []
        for event in late:
            response = await h.webhook(event)
            assert response.status_code == 200, response.text
            outcomes.append(response.json()["outcome"])
        deleted = customer_event(
            "customer.deleted",
            customer_id=customer_id,
            account_id=account_id,
            email="lukas@example.org",
            name="Lukas Example",
        )
        ignored = await h.webhook(deleted)

        assert outcomes == ["unknown_account"] * len(late)
        assert (ignored.status_code, ignored.json()["outcome"]) == (200, "ignored")
        assert await rows_of(store, account_id) == {}
        assert len(h.entitlements.requests) == plans_set
        assert _changes(h) == published
        async with store.transaction() as tx:
            assert await tx.find_customer("fake", customer_id) is None
            assert await tx.get_subscription("fake", "sub_1") is None
            # The ledger remembers that the events came, and nothing of whom they were about.
            for event in [*late, deleted]:
                row = await tx.get_event("fake", event["id"])
                assert row is not None and row.account_id is None


async def test_an_event_naming_an_account_in_its_metadata_needs_a_known_customer() -> None:
    """The rule the late events rely on, by itself: Billing created every customer it bills, so
    an event of a customer it does not know is nobody's, whatever its metadata says."""
    async with harness() as h:
        account_id = uuid4()
        event = _subscription_event(h, account_id, "sub_x", "active", "created")

        response = await h.webhook(event)

        assert response.json()["outcome"] == "unknown_account"
        assert h.entitlements.requests == []
        assert await rows_of(h.store, account_id) == {}


class SlowEntitlements:
    """An ``EntitlementsGateway`` that answers when the test lets it."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.requests: list[PlanSetRequest] = []

    async def set_plan(self, request: PlanSetRequest) -> PlanSetReply:
        self.requests.append(request)
        self.started.set()
        await self.release.wait()
        return PlanSetReply(ok=True, plan=request.plan, status=request.status)


@pytest.mark.parametrize("kind", STORES)
async def test_an_event_that_is_being_applied_finishes_before_the_rows_go(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider event can arrive while the erasure runs (a renewal, or the provider's answer
    to the cancellation, within moments). The event holds the customer row; the erasure waits
    for it. So the plan it sets is set before Billing confirms (and Identity asks Entitlements
    only after that), and the row it writes is deleted with the rest."""
    async with store_of(kind, monkeypatch) as store, harness(store=store) as h:  # type: ignore[arg-type]
        account_id = await subscribe(h, "Garden", "sub_1")
        slow = SlowEntitlements()
        h.service.entitlements = slow
        h.clock.advance(seconds=5)
        renewed = _subscription_event(h, account_id, "sub_1", "active")

        applying = asyncio.create_task(h.webhook(renewed))
        await asyncio.wait_for(slow.started.wait(), timeout=5)
        erasing = asyncio.create_task(erase(h, account_id))
        await asyncio.sleep(0.3)
        waited = not erasing.done()
        slow.release.set()
        applied = await asyncio.wait_for(applying, timeout=5)
        reply = await asyncio.wait_for(erasing, timeout=5)

        assert waited, "the erasure deleted the rows while an event was being applied"
        assert applied.json()["outcome"] == "applied"
        assert reply.ok
        assert [r.account_id for r in slow.requests] == [account_id]
        assert await rows_of(store, account_id) == {}
        async with store.transaction() as tx:
            ledger = await tx.get_event("fake", renewed["id"])
        assert ledger is None or ledger.account_id is None


# --- the installation told Identity ------------------------------------------------------------


def test_billing_refuses_to_start_where_identity_would_not_ask_it() -> None:
    """Compose passes ``COMPOSE_PROFILES`` to Identity and to Billing; ``--profile billing``
    on the command line alone leaves it empty, and Identity would erase accounts without
    Billing."""
    ensure_identity_asks(make_settings(billing_optional_services="billing"))
    ensure_identity_asks(make_settings(billing_optional_services="matter, billing ,relay"))
    for missing in ("", "matter,relay", "billing-migrate"):
        with pytest.raises(ValueError, match="COMPOSE_PROFILES=billing"):
            ensure_identity_asks(make_settings(billing_optional_services=missing))
