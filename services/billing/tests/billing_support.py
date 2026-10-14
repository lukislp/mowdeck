"""Helpers for the billing tests (a plain module: the repository has no conftest.py).

``harness()`` builds the real app with a memory store, the fake payment provider, the in-memory
OpenFGA fake (it evaluates the packaged model), an in-memory bus on which a fake Entitlements
answers ``entitlements.plan.set``, access tokens minted with ``TestKeys`` and a manual clock.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI
from mowdeck_auth.testing import TestKeys, bearer
from mowdeck_authz import RelationshipTuple, TupleWriter, account, principal_subject
from mowdeck_authz.testing import InMemoryAuthorizer
from mowdeck_contracts.v1 import (
    PlanSetError,
    PlanSetReply,
    PlanSetRequest,
    Principal,
    PrincipalKind,
)
from mowdeck_contracts.v1.subjects import ENTITLEMENTS_PLAN_SET
from mowdeck_service_kit.bus import BusMessage, InMemoryBus
from mowdeck_service_kit.testing import lifespan_client

from billing.adapters.memory import MemoryStore
from billing.adapters.plans import StaticPlanCatalog
from billing.adapters.providers.fake import FakeProvider
from billing.adapters.providers.fixtures import encode, sign
from billing.application.billing import BillingService
from billing.domain.models import OfferedPlan
from billing.domain.prices import PriceTable
from billing.main import build_app
from billing.settings import Settings

API = "/api/v1/billing"
WEBHOOK = f"{API}/webhooks/stripe"
SECRET = "whsec_test_secret"  # noqa: S105 - test value
T0 = datetime(2026, 10, 10, 8, 0, tzinfo=UTC)
PRICES = PriceTable(currency="eur", prices={"home": "price_home", "fleet": "price_fleet", "x": ""})
PLANS = [
    OfferedPlan(key="home", name="Home", description="One garden", trial_days=30),
    OfferedPlan(key="fleet", name="Fleet", trial_days=14),
]


class ManualClock:
    """A clock the test moves forward."""

    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> datetime:
        """Move forward (``days=5``) and return the new time."""
        self.now += timedelta(**delta)
        return self.now

    @property
    def ts(self) -> int:
        """The current time as a Unix timestamp."""
        return int(self.now.timestamp())


def make_settings(**overrides: Any) -> Settings:
    """Settings for tests (environment independent)."""
    values: dict[str, Any] = {
        "environment": "test",
        "log_format": "console",
        "nats_enabled": False,
        "billing_provider": "fake",
        "billing_stripe_webhook_secret": SECRET,
        "billing_entitlements_url": "",
        **overrides,
    }
    return Settings.model_validate(values)


@dataclass
class FakeEntitlements:
    """Answers ``entitlements.plan.set`` on the in-memory bus and records the requests."""

    requests: list[PlanSetRequest] = field(default_factory=list)
    known_plans: set[str] = field(default_factory=lambda: {"home", "fleet"})
    silent: bool = False

    async def handle(self, message: BusMessage) -> None:
        """Record and answer (unless silent: then the request times out)."""
        request = message.model(PlanSetRequest)
        if self.silent:
            return
        self.requests.append(request)
        if request.plan not in self.known_plans:
            await message.respond(PlanSetReply(ok=False, error=PlanSetError.UNKNOWN_PLAN))
            return
        await message.respond(PlanSetReply(ok=True, plan=request.plan, status=request.status))


@dataclass
class Person:
    """A principal with its access token."""

    principal: Principal
    token: str

    @property
    def headers(self) -> dict[str, str]:
        """Bearer header."""
        return bearer(self.token)

    @property
    def account_id(self) -> UUID:
        """The account of the token."""
        assert self.principal.account_id is not None
        return self.principal.account_id


@dataclass
class Harness:
    """The app under test and its fakes."""

    client: httpx.AsyncClient
    app: FastAPI
    store: MemoryStore
    provider: FakeProvider
    authz: InMemoryAuthorizer
    bus: InMemoryBus
    keys: TestKeys
    clock: ManualClock
    entitlements: FakeEntitlements
    identity: TupleWriter = field(init=False)

    def __post_init__(self) -> None:
        self.identity = TupleWriter(self.authz, owner="identity")

    @property
    def service(self) -> BillingService:
        """The wired use cases."""
        service: BillingService = self.app.state.billing_service
        return service

    def person(self, account_id: UUID | None = None) -> Person:
        """A principal with a minted token (no tuples)."""
        principal = Principal(
            subject=uuid4(), account_id=account_id or uuid4(), kind=PrincipalKind.USER
        )
        return Person(principal, self.keys.mint(principal))

    async def member(self, relation: str, account_id: UUID | None = None) -> Person:
        """A user with ``relation`` (owner, admin, member) on an account."""
        person = self.person(account_id)
        await self.identity.write(
            [
                RelationshipTuple(
                    principal_subject(person.principal), relation, account(person.account_id)
                )
            ]
        )
        return person

    async def owner(self) -> Person:
        """A new account and its owner."""
        return await self.member("owner")

    async def webhook(self, event: dict[str, Any], secret: str = SECRET) -> httpx.Response:
        """Post a signed event like Stripe."""
        body = encode(event)
        return await self.client.post(
            WEBHOOK,
            content=body,
            headers={"Stripe-Signature": sign(body, secret), "Content-Type": "application/json"},
        )


@asynccontextmanager
async def harness(
    *,
    store: MemoryStore | None = None,
    provider: FakeProvider | None = None,
    prices: PriceTable | None = None,
    plans: list[OfferedPlan] | None = None,
    entitlements_timeout: float = 0.5,
    **overrides: Any,
) -> AsyncIterator[Harness]:
    """The app with fakes, inside its lifespan."""
    memory = store or MemoryStore()
    fake = provider or FakeProvider(webhook_secret=SECRET)
    authz = InMemoryAuthorizer()
    bus = InMemoryBus()
    keys = TestKeys()
    clock = ManualClock()
    entitlements = FakeEntitlements()
    await bus.subscribe(ENTITLEMENTS_PLAN_SET, entitlements.handle, queue="entitlements")
    app = build_app(
        make_settings(billing_entitlements_timeout_seconds=entitlements_timeout, **overrides),
        bus=bus,
        store=memory,
        provider=fake,
        authorizer=authz,
        principal_resolver=keys.resolver(),
        catalog=StaticPlanCatalog(PLANS if plans is None else plans),
        prices=PRICES if prices is None else prices,
        clock=clock,
    )
    async with lifespan_client(app) as client:
        yield Harness(client, app, memory, fake, authz, bus, keys, clock, entitlements)
