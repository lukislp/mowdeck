# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Account erasure with Billing and Entitlements (ADR 0082, step 2c): the three real apps on one
bus. Identity purges a deleted account that pays for a plan; Billing ends the subscription and
deletes the customer at the (fake) payment provider; Entitlements deletes the plan. The point
of the test is what must not happen: a plan row of the erased account coming back.

``entitlements.plan.set`` creates the plan row of any account, and Billing sends it whenever the
provider reports a subscription. So Identity asks Billing before Entitlements (the domain's
``ASK_ORDER``), and Billing applies no provider event to a customer it no longer knows.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from identity_support import PASSWORD, Account, Harness, harness
from mowdeck_auth.testing import TestKeys
from mowdeck_authz.testing import InMemoryAuthorizer
from mowdeck_contracts.v1 import SubscriptionStatus
from mowdeck_contracts.v1.subjects import ACCOUNT_ERASE_PREFIX
from mowdeck_service_kit.bus import InMemoryBus
from mowdeck_service_kit.testing import lifespan_client

from billing.adapters.memory import MemoryStore as BillingStore
from billing.adapters.plans import StaticPlanCatalog
from billing.adapters.providers.fake import FakeProvider, fake_customer_id
from billing.adapters.providers.fixtures import encode, sign, subscription_event
from billing.application.billing import BillingService
from billing.domain.models import ProviderSubscription
from billing.domain.prices import PriceTable
from billing.main import build_app as build_billing
from billing.settings import Settings as BillingSettings
from entitlements.adapters.memory import MemoryStore as EntitlementsStore
from entitlements.domain.planfile import parse_plan_file
from entitlements.main import build_app as build_entitlements
from entitlements.settings import Settings as EntitlementsSettings
from identity.application.erasure import ErasureService
from identity.domain.erasure import AccountErasure, ErasureState

SECRET = "whsec_test_secret"  # noqa: S105 - test value
WEBHOOK = "/api/v1/billing/webhooks/stripe"
PLANS = Path(__file__).resolve().parents[3] / "infra" / "entitlements" / "plans.yaml"
EXPIRED = timedelta(minutes=11)
"""The access token lifetime plus the skew: nothing is asked before (``test_identity_erasure``)."""
COMMON: dict[str, Any] = {"environment": "test", "log_format": "console", "nats_enabled": False}
PURGE: dict[str, Any] = {
    "identity_deletion_grace_days": 0,
    # Listed the wrong way round on purpose: the order asked is the domain's.
    "identity_erasure_services": "entitlements",
    "identity_erasure_settle_seconds": 30,
}


@dataclass
class Stack:
    """Identity, Billing and Entitlements on one bus."""

    identity: Harness
    billing: httpx.AsyncClient | None
    billing_app: FastAPI
    billing_store: BillingStore
    provider: FakeProvider
    entitlements_store: EntitlementsStore
    asked: list[str]
    """The erasure subjects Identity requested, in order."""

    async def plan_rows(self, account_id: UUID) -> dict[str, int]:
        """What Entitlements holds of the account."""
        async with self.entitlements_store.transaction() as tx:
            return await tx.account_rows(account_id)

    async def billing_rows(self, account_id: UUID) -> dict[str, int]:
        """What Billing holds of the account."""
        async with self.billing_store.transaction() as tx:
            return await tx.account_rows(account_id)

    async def webhook(self, event: dict[str, Any]) -> str:
        """A signed provider event; the outcome Billing answers."""
        assert self.billing is not None
        body = encode(event)
        response = await self.billing.post(
            WEBHOOK,
            content=body,
            headers={"Stripe-Signature": sign(body, SECRET), "Content-Type": "application/json"},
        )
        assert response.status_code == 200, response.text
        outcome: str = response.json()["outcome"]
        return outcome

    async def subscribed(self, account: Account, subscription_id: str) -> None:
        """The account buys the plan ``home``: a checkout (the customer at the provider) and the
        provider's ``customer.subscription.created``."""
        assert self.billing is not None
        service: BillingService = self.billing_app.state.billing_service
        await service.start_checkout(account.account_id, "home", "https://app.example.org")
        self.provider.subscriptions[subscription_id] = ProviderSubscription(
            id=subscription_id,
            customer_id=fake_customer_id(account.account_id),
            status=SubscriptionStatus.ACTIVE,
        )
        event = _subscription(account.account_id, subscription_id, "active", "created")
        assert await self.webhook(event) == "applied"

    async def purge(self, account: Account) -> AccountErasure:
        """The owner deletes the account and the purge runs to its end (or as far as it gets)."""
        h = self.identity
        response = await h.client.request(
            "DELETE", account.url(), headers=account.headers, json={"password": PASSWORD}
        )
        assert response.status_code == 202, response.text
        erasure: ErasureService = h.app.state.identity.erasure
        await erasure.begin_due()
        h.clock.advance(EXPIRED)
        for _ in range(4):  # the first service, the settle time, the others, the event stream
            await erasure.sweep()
            h.clock.advance(timedelta(seconds=31))
        async with h.store.transaction() as tx:
            record = await tx.erasures.get(account_id=account.account_id)
        assert record is not None
        return record


def _subscription(account_id: UUID, subscription_id: str, status: str, kind: str) -> dict[str, Any]:
    """A ``customer.subscription.*`` event as Stripe sends it for a subscription of Billing's
    checkout: the metadata names the account and the plan."""
    return subscription_event(
        f"customer.subscription.{kind}",
        subscription_id=subscription_id,
        customer_id=fake_customer_id(account_id),
        status=status,
        price_id="price_home",
        account_id=account_id,
        plan="home",
        period_start=1_800_000_000,
        period_end=1_802_600_000,
    )


@asynccontextmanager
async def stack(
    monkeypatch: pytest.MonkeyPatch, *, billing: bool, optional_services: str
) -> AsyncIterator[Stack]:
    """The apps inside their lifespans. Without ``billing`` the installation does not run it."""
    bus = InMemoryBus()
    asked: list[str] = []
    request = bus.request

    async def recording(subject: str, *args: Any, **kwargs: Any) -> Any:
        if subject.startswith(f"{ACCOUNT_ERASE_PREFIX}."):
            asked.append(subject)
        return await request(subject, *args, **kwargs)

    monkeypatch.setattr(bus, "request", recording)
    keys = TestKeys()
    entitlements_store = EntitlementsStore()
    entitlements_app = build_entitlements(
        EntitlementsSettings.model_validate({**COMMON, "entitlements_billing_enabled": True}),
        bus=bus,
        store=entitlements_store,
        authorizer=InMemoryAuthorizer(),
        principal_resolver=keys.resolver(),
        plan_specs=parse_plan_file(PLANS.read_text(encoding="utf-8")),
        background_tasks=False,
    )
    billing_store = BillingStore()
    provider = FakeProvider(webhook_secret=SECRET)
    billing_app = build_billing(
        BillingSettings.model_validate(
            {
                **COMMON,
                "billing_provider": "fake",
                "billing_stripe_webhook_secret": SECRET,
                "billing_ledger_key": "ledger-key-for-tests-only-0123456789",
                "billing_entitlements_url": "",
                "billing_optional_services": optional_services,
            }
        ),
        bus=bus,
        store=billing_store,
        provider=provider,
        authorizer=InMemoryAuthorizer(),
        principal_resolver=keys.resolver(),
        catalog=StaticPlanCatalog([]),
        prices=PriceTable(currency="eur", prices={"home": "price_home"}),
    )
    settings = {**PURGE, "identity_optional_services": optional_services}
    async with lifespan_client(entitlements_app):
        if not billing:
            async with harness(bus=bus, **settings) as h:
                yield Stack(
                    h, None, billing_app, billing_store, provider, entitlements_store, asked
                )
            return
        async with lifespan_client(billing_app) as client, harness(bus=bus, **settings) as h:
            yield Stack(h, client, billing_app, billing_store, provider, entitlements_store, asked)


async def test_no_plan_comes_back_after_an_account_with_a_subscription_is_erased(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with stack(monkeypatch, billing=True, optional_services="billing") as s:
        lukas = await s.identity.register("lukas")
        bob = await s.identity.register("bob")
        await s.subscribed(lukas, "sub_lukas")
        await s.subscribed(bob, "sub_bob")
        customer_id = fake_customer_id(lukas.account_id)
        # Both services hold the account before: nothing below is checked empty by accident.
        assert await s.plan_rows(lukas.account_id) != {}
        assert set(await s.billing_rows(lukas.account_id)) >= {"customers", "subscriptions"}
        bob_plan, bob_billing = (
            await s.plan_rows(bob.account_id),
            await s.billing_rows(bob.account_id),
        )

        record = await s.purge(lukas)
        # What the provider sends after the erasure: its answer to the cancellation, with the
        # account in the metadata, and a renewal that was already on its way.
        late = [
            await s.webhook(_subscription(lukas.account_id, "sub_lukas", "canceled", "deleted")),
            await s.webhook(_subscription(lukas.account_id, "sub_lukas", "active", "updated")),
        ]
        async with s.identity.store.transaction() as tx:
            account = await tx.accounts.get(id=lukas.account_id)

        assert record.state is ErasureState.ERASED and account is None
        # The order is the domain's, although the setting listed Entitlements first.
        assert s.asked == ["account.erase.billing", "account.erase.entitlements"]
        assert record.confirmed == ["billing", "entitlements"]
        # At the provider: the subscription is over, the customer is deleted, each once.
        assert s.provider.calls == [
            ("cancel_subscription", "sub_lukas"),
            ("delete_customer", customer_id),
        ]
        assert customer_id not in s.provider.customers
        # Neither service holds anything of the account, and the late events changed nothing.
        assert late == ["unknown_account", "unknown_account"]
        assert await s.billing_rows(lukas.account_id) == {}
        assert await s.plan_rows(lukas.account_id) == {}
        # The other account keeps its subscription, its customer and its plan.
        assert await s.plan_rows(bob.account_id) == bob_plan
        assert await s.billing_rows(bob.account_id) == bob_billing
        assert s.provider.subscriptions["sub_bob"].status is SubscriptionStatus.ACTIVE


async def test_an_installation_without_billing_erases_without_asking_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Most installations do not run Billing: their deployment names no optional service, and
    the purge completes with the services that exist."""
    async with stack(monkeypatch, billing=False, optional_services="") as s:
        lukas = await s.identity.register("lukas")
        assert await s.plan_rows(lukas.account_id) != {}

        record = await s.purge(lukas)

        assert record.state is ErasureState.ERASED
        assert s.asked == ["account.erase.entitlements"]
        assert await s.plan_rows(lukas.account_id) == {}


async def test_billing_that_does_not_answer_holds_the_purge_and_entitlements_is_not_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deployment says Billing runs and it is down: Identity keeps the account locked and
    asks again. It does not go on to Entitlements, and it does not delete its own rows."""
    async with stack(monkeypatch, billing=False, optional_services="billing") as s:
        lukas = await s.identity.register("lukas")

        record = await s.purge(lukas)
        async with s.identity.store.transaction() as tx:
            account = await tx.accounts.get(id=lukas.account_id)

        assert record.state is ErasureState.PURGING and account is not None
        assert record.open_services == ["billing", "entitlements"]
        assert set(s.asked) == {"account.erase.billing"}
        assert record.last_error is not None and record.last_error.startswith("billing: ")
        assert await s.plan_rows(lukas.account_id) != {}
