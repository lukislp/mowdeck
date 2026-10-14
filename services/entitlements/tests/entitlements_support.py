"""Helpers for the entitlements tests (a plain module: the repository has no conftest.py).

``harness()`` builds the real app with a memory store, the in-memory OpenFGA fake (it evaluates
the packaged model), an in-memory bus, access tokens minted with ``TestKeys``, a manual clock
and the plans of the repository's ``infra/entitlements/plans.yaml``.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI
from mowdeck_auth.testing import TestKeys, bearer
from mowdeck_authz import (
    PLATFORM_OBJECT,
    RelationshipTuple,
    TupleWriter,
    account,
    principal_subject,
    user,
)
from mowdeck_authz.testing import InMemoryAuthorizer
from mowdeck_contracts.v1 import (
    IDENTITY_ACCOUNT_CREATED,
    AccountCreated,
    AccountKind,
    EventEnvelope,
    Principal,
    PrincipalKind,
)
from mowdeck_service_kit.bus import InMemoryBus, publish_event
from mowdeck_service_kit.testing import lifespan_client

from entitlements.adapters.memory import MemoryStore
from entitlements.application.services import Services
from entitlements.domain.planfile import PlanSpec, parse_plan_file
from entitlements.main import build_app
from entitlements.settings import Settings

API = "/api/v1/entitlements"
T0 = datetime(2026, 10, 10, 8, 0, tzinfo=UTC)
PLANS_FILE = Path(__file__).resolve().parents[3] / "infra" / "entitlements" / "plans.yaml"


def repository_plans() -> list[PlanSpec]:
    """The plans shipped in ``infra/entitlements/plans.yaml``."""
    return parse_plan_file(PLANS_FILE.read_text(encoding="utf-8"))


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


def make_settings(**overrides: Any) -> Settings:
    """Settings for tests (environment independent)."""
    values: dict[str, Any] = {
        "environment": "test",
        "log_format": "console",
        "nats_enabled": False,
        **overrides,
    }
    return Settings.model_validate(values)


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
    authz: InMemoryAuthorizer
    bus: InMemoryBus
    keys: TestKeys
    clock: ManualClock
    identity: TupleWriter = field(init=False)

    def __post_init__(self) -> None:
        self.identity = TupleWriter(self.authz, owner="identity")

    @property
    def services(self) -> Services:
        """The wired use cases."""
        services: Services = self.app.state.entitlements_services
        return services

    def person(
        self, account_id: UUID | None = None, *, kind: PrincipalKind = PrincipalKind.USER
    ) -> Person:
        """A principal with a minted token (no tuples)."""
        principal = Principal(subject=uuid4(), account_id=account_id or uuid4(), kind=kind)
        return Person(principal, self.keys.mint(principal))

    async def owner(self) -> Person:
        """A new account and its owner (``account#owner``, ``account#member``)."""
        person = self.person()
        subject = principal_subject(person.principal)
        await self.identity.write(
            [
                RelationshipTuple(subject, "owner", account(person.account_id)),
                RelationshipTuple(subject, "member", account(person.account_id)),
            ]
        )
        return person

    async def operator(self) -> Person:
        """An owner who is also an operator admin (``platform:main#operator_admin``)."""
        person = await self.owner()
        await self.identity.write(
            [RelationshipTuple(user(person.principal.subject), "operator_admin", PLATFORM_OBJECT)]
        )
        return person

    async def account_created(self, account_id: UUID, name: str = "Garden") -> None:
        """Publish ``identity.account.created`` like Identity."""
        await publish_event(
            self.bus,
            EventEnvelope[AccountCreated](
                type=IDENTITY_ACCOUNT_CREATED,
                producer="identity",
                account_id=account_id,
                payload=AccountCreated(
                    account_id=account_id,
                    name=name,
                    kind=AccountKind.HOUSEHOLD,
                    owner_user_id=uuid4(),
                ),
            ),
        )

    async def me(self, person: Person) -> dict[str, Any]:
        """``GET /me``; asserts 200."""
        response = await self.client.get(f"{API}/me", headers=person.headers)
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        return body

    async def assign(self, operator: Person, account_id: UUID, **body: Any) -> httpx.Response:
        """``PUT /accounts/{id}/plan``."""
        values = {"reason": "test", **body}
        return await self.client.put(
            f"{API}/accounts/{account_id}/plan", headers=operator.headers, json=values
        )


@asynccontextmanager
async def harness(
    *,
    store: Any = None,
    bus: Any = None,
    plan_specs: list[PlanSpec] | None = None,
    module_source: Any = None,
    **overrides: Any,
) -> AsyncIterator[Harness]:
    """The app with fakes, inside its lifespan."""
    memory = store or MemoryStore()
    authz = InMemoryAuthorizer()
    active_bus = bus or InMemoryBus()
    keys = TestKeys()
    clock = ManualClock()
    app = build_app(
        make_settings(**overrides),
        bus=active_bus,
        store=memory,
        authorizer=authz,
        principal_resolver=keys.resolver(),
        module_source=module_source,
        clock=clock,
        plan_specs=repository_plans() if plan_specs is None else plan_specs,
        background_tasks=False,
    )
    async with lifespan_client(app) as client:
        yield Harness(client, app, memory, authz, active_bus, keys, clock)
