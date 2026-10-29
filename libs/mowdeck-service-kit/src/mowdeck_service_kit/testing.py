"""Test utilities for services built on the kit (install the ``testing`` extra for httpx).

Typical test::

    async def test_foreign_mower_is_invisible() -> None:
        owner = make_principal(permissions=["mower.view"])
        intruder = make_principal(permissions=["mower.view"])  # another account
        app = make_test_app(routers=[mowers.router], principal=intruder)
        async with lifespan_client(app) as client:
            response = await client.get(f"/api/v1/mowers/{mower_of(owner)}")
        assert response.status_code == 404  # never reveal foreign resources

Cross-tenant tests are mandatory for every endpoint that touches account data.
"""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, FastAPI, Request
from mowdeck_contracts.v1 import (
    ALL_FEATURES,
    EntitlementCheckReply,
    EntitlementLimitQuery,
    EntitlementSnapshot,
    ModuleDescriptor,
    PermissionDef,
    PlanLimits,
    PlanStatus,
    Principal,
    PrincipalKind,
)

from mowdeck_service_kit.app import LifecycleHook, create_app
from mowdeck_service_kit.bus import EventBus, InMemoryBus
from mowdeck_service_kit.demo import DemoPolicy
from mowdeck_service_kit.errors import Unauthorized
from mowdeck_service_kit.health import CheckFunction, ReadinessCheck
from mowdeck_service_kit.settings import ServiceSettings
from mowdeck_service_kit.tenancy import PrincipalResolver

__all__ = [
    "FakeEntitlements",
    "InMemoryBus",
    "StaticPrincipalResolver",
    "lifespan_client",
    "make_principal",
    "make_test_app",
    "make_test_descriptor",
    "make_test_settings",
]


class FakeEntitlements:
    """An ``Entitlements`` for tests: one plan for every account, changeable between requests.

    ``FakeEntitlements()`` allows everything (plan ``unlimited``). Set ``features``, ``limits``
    or ``status`` to exercise the enforcement points; ``checks`` records every question as
    ``("feature", key)`` or ``("limit", key, current)``.
    """

    def __init__(
        self,
        *,
        plan: str = "unlimited",
        features: Sequence[str] = (ALL_FEATURES,),
        limits: PlanLimits | None = None,
        status: PlanStatus = PlanStatus.ACTIVE,
    ) -> None:
        self.plan = plan
        self.features = list(features)
        self.limits = limits or PlanLimits()
        self.status = status
        self.checks: list[tuple[Any, ...]] = []
        self.invalidated: list[UUID | None] = []

    def snapshot(self, account_id: UUID) -> EntitlementSnapshot:
        """The snapshot every account resolves to."""
        return EntitlementSnapshot(
            account_id=account_id,
            plan=self.plan,
            status=self.status,
            limits=self.limits,
            features=self.features,
            billing_enabled=True,
        )

    async def check_feature(self, account_id: UUID, feature: str) -> EntitlementCheckReply:
        """Decide a feature question."""
        self.checks.append(("feature", feature))
        return self.snapshot(account_id).decide(feature=feature)

    async def check_limit(self, account_id: UUID, key: str, current: int) -> EntitlementCheckReply:
        """Decide a limit question."""
        self.checks.append(("limit", key, current))
        query = EntitlementLimitQuery.model_validate({"key": key, "current": current})
        return self.snapshot(account_id).decide(limit=query)

    async def plan_of(self, account_id: UUID) -> str | None:
        """The plan key."""
        return self.plan

    def invalidate(self, account_id: UUID | None = None) -> None:
        """Record the invalidation."""
        self.invalidated.append(account_id)


class StaticPrincipalResolver:
    """Resolves every request to a fixed principal, or 401 when it is None.

    ``principal`` may be reassigned between requests to act as different users.
    """

    def __init__(self, principal: Principal | None) -> None:
        self.principal = principal

    async def resolve(self, request: Request) -> Principal:
        """Return the configured principal or raise :class:`Unauthorized`."""
        if self.principal is None:
            raise Unauthorized("no principal configured")
        return self.principal


def make_principal(
    *,
    account_id: UUID | None = None,
    permissions: Sequence[str] = (),
    roles: Sequence[str] = (),
    kind: PrincipalKind = PrincipalKind.USER,
    with_account: bool = True,
    **extra: Any,
) -> Principal:
    """Build a principal; each call gets a fresh account unless ``account_id`` is given."""
    resolved_account = account_id if account_id is not None else (uuid4() if with_account else None)
    return Principal(
        subject=uuid4(),
        account_id=resolved_account,
        kind=kind,
        roles=list(roles),
        permissions=list(permissions),
        **extra,
    )


def make_test_settings(**overrides: Any) -> ServiceSettings:
    """Settings for tests: no NATS, console logs, environment ``test``.

    The environment and secret files are ignored, so tests behave the same on every machine.
    """
    values: dict[str, Any] = {
        "service_name": "test-service",
        "environment": "test",
        "log_format": "console",
        "nats_enabled": False,
        "metrics_enabled": True,
        "shutdown_grace_seconds": 2.0,
        **overrides,
    }
    return ServiceSettings.model_validate(values)


def make_test_descriptor(name: str = "test-service", **overrides: Any) -> ModuleDescriptor:
    """A minimal module descriptor."""
    values: dict[str, Any] = {
        "name": name,
        "version": "0.1.0",
        "permissions": [PermissionDef(key="mower.view", description="View mowers")],
        **overrides,
    }
    return ModuleDescriptor(**values)


def make_test_app(
    *,
    routers: Sequence[APIRouter] = (),
    principal: Principal | None = None,
    principal_resolver: PrincipalResolver | None = None,
    bus: EventBus | None = None,
    settings: ServiceSettings | None = None,
    descriptor: ModuleDescriptor | None = None,
    readiness_checks: Sequence[ReadinessCheck | CheckFunction] = (),
    on_startup: Sequence[LifecycleHook] = (),
    on_shutdown: Sequence[LifecycleHook] = (),
    demo_policy: DemoPolicy | None = None,
) -> FastAPI:
    """Build an app for tests with an :class:`InMemoryBus` and a static principal."""
    return create_app(
        settings or make_test_settings(),
        descriptor or make_test_descriptor(),
        routers=routers,
        readiness_checks=readiness_checks,
        on_startup=on_startup,
        on_shutdown=on_shutdown,
        bus=bus if bus is not None else InMemoryBus(),
        principal_resolver=principal_resolver or StaticPrincipalResolver(principal),
        demo_policy=demo_policy,
    )


@asynccontextmanager
async def lifespan_client(
    app: FastAPI, *, base_url: str = "http://testserver"
) -> AsyncIterator[httpx.AsyncClient]:
    """Run the app's lifespan (startup and shutdown hooks) around an ``httpx.AsyncClient``."""
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=base_url) as client:
            yield client
