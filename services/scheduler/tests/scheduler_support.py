"""Helpers for the scheduler tests (a plain module: the repository has no conftest.py).

``harness()`` builds the real Scheduler app *and* the real Fleet app on one in-memory bus and
one in-memory OpenFGA fake (it evaluates the packaged model), with memory stores, access tokens
minted with ``mowdeck_auth.testing.TestKeys`` (both apps accept them), a manual clock for the
Scheduler and the static weather provider. Fleet answers ``fleet.targets.resolve`` and
``fleet.commands.issue`` for real, and a fake Device-Gateway answers every command with
``accepted`` and ``executed``, so a firing is tested end to end down to the command record.
"""

from collections.abc import AsyncIterator, Iterable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI
from mowdeck_auth.testing import TestKeys, bearer
from mowdeck_authz import ObjectRef, RelationshipTuple, TupleWriter, account, principal_subject
from mowdeck_authz.testing import InMemoryAuthorizer
from mowdeck_contracts.v1 import (
    CommandEnvelope,
    CommandResult,
    CommandStatus,
    ConnectionKind,
    EventEnvelope,
    GeoPosition,
    GeoReference,
    MapArea,
    MowerActivity,
    MowerCapabilities,
    MowerCapabilitiesSnapshot,
    MowerMap,
    MowerMapSnapshot,
    MowerState,
    Principal,
    PrincipalKind,
)
from mowdeck_contracts.v1.subjects import ALL_COMMANDS, device_subject, telemetry_subject
from mowdeck_service_kit.bus import BusMessage, InMemoryBus, publish_command_result
from mowdeck_service_kit.testing import lifespan_client

from fleet.adapters.memory import MemoryStore as FleetStore
from fleet.main import build_app as build_fleet
from fleet.settings import Settings as FleetSettings
from scheduler.adapters.memory import MemoryStore
from scheduler.adapters.weather.static import StaticWeatherProvider
from scheduler.application.services import Services
from scheduler.main import build_app
from scheduler.settings import Settings

API = "/api/v1/scheduler"
FLEET = "/api/v1/fleet"
T0 = datetime(2026, 10, 9, 8, 0, tzinfo=UTC)
"""A Friday, 10:00 in Europe/Berlin (CEST)."""

ROLE_KEYS: dict[str, tuple[str, ...]] = {
    "manager": (
        "mower.view",
        "mower.control",
        "schedule.edit",
        "fleet.bulk_command",
        "fleet.manage",
    ),
    "operator": ("mower.view", "mower.control", "mower.drive"),
    "scheduler": ("mower.view", "schedule.edit"),
    "viewer": ("mower.view",),
}
"""Identity's default roles plus a role that may edit but not control."""


class ManualClock:
    """A clock the test moves forward."""

    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> datetime:
        """Move forward (``minutes=5``) and return the new time."""
        self.now += timedelta(**delta)
        return self.now

    def set(self, value: datetime) -> datetime:
        """Jump to ``value``."""
        self.now = value
        return self.now


def make_settings(**overrides: Any) -> Settings:
    """Settings for tests (environment independent)."""
    values: dict[str, Any] = {
        "environment": "test",
        "log_format": "console",
        "nats_enabled": False,
        "scheduler_weather_provider": "static",
        **overrides,
    }
    return Settings.model_validate(values)


@dataclass
class Person:
    """A principal of an account with its access token."""

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
    """The apps under test and their fakes."""

    client: httpx.AsyncClient
    fleet_client: httpx.AsyncClient
    store: MemoryStore
    fleet_store: FleetStore
    authz: InMemoryAuthorizer
    bus: InMemoryBus
    keys: TestKeys
    app: FastAPI
    fleet_app: FastAPI
    clock: ManualClock
    weather: StaticWeatherProvider
    identity: TupleWriter = field(init=False)
    commands: list[CommandEnvelope] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.identity = TupleWriter(self.authz, owner="identity")

    @property
    def services(self) -> Services:
        """The wired use cases of the Scheduler."""
        services: Services = self.app.state.scheduler
        return services

    def person(
        self,
        account_id: UUID,
        *,
        kind: PrincipalKind = PrincipalKind.USER,
        permissions: Iterable[str] = (),
    ) -> Person:
        """A principal with a minted token (no tuples)."""
        principal = Principal(
            subject=uuid4(), account_id=account_id, kind=kind, permissions=list(permissions)
        )
        return Person(principal, self.keys.mint(principal))

    async def owner(self) -> Person:
        """A new account and its owner (``account#owner``, ``account#member``)."""
        person = self.person(
            uuid4(), permissions=sorted({k for v in ROLE_KEYS.values() for k in v})
        )
        subject = principal_subject(person.principal)
        await self.identity.write(
            [
                RelationshipTuple(subject, "owner", account(person.account_id)),
                RelationshipTuple(subject, "member", account(person.account_id)),
            ]
        )
        return person

    async def member(self, owner: Person, role: str, level: ObjectRef | None = None) -> Person:
        """A sub-user of ``owner``'s account with ``role`` at ``level`` (default: the account)."""
        person = self.person(
            owner.account_id, kind=PrincipalKind.SUBUSER, permissions=ROLE_KEYS[role]
        )
        subject = principal_subject(person.principal)
        await self.identity.write([RelationshipTuple(subject, "member", account(owner.account_id))])
        await self.identity.replace_subject_grants(
            subject, level or account(owner.account_id), ROLE_KEYS[role]
        )
        return person

    async def revoke(self, person: Person, level: ObjectRef | None = None) -> None:
        """Take every grant of ``person`` at ``level`` away."""
        await self.identity.replace_subject_grants(
            principal_subject(person.principal), level or account(person.account_id), []
        )

    async def fleet_post(self, person: Person, path: str, **body: Any) -> dict[str, Any]:
        """``POST /api/v1/fleet/<path>``; asserts 201."""
        response = await self.fleet_client.post(
            f"{FLEET}/{path}", headers=person.headers, json=body
        )
        assert response.status_code == 201, response.text
        result: dict[str, Any] = response.json()
        return result

    async def register(self, owner: Person, **body: Any) -> UUID:
        """Register a simulated mower in Fleet; returns its id."""
        created = await self.fleet_post(
            owner,
            "mowers",
            **{"name": f"mower-{uuid4().hex[:6]}", "connection_kind": "simulated", **body},
        )
        return UUID(created["id"])

    async def create(self, person: Person, **body: Any) -> httpx.Response:
        """``POST /schedules`` with sensible defaults (weekly, every day, 10:30)."""
        payload: dict[str, Any] = {
            "name": "Morning",
            "kind": "weekly",
            "days_of_week": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
            "start_time": "10:30",
            "duration_minutes": 120,
            "area_ids": ["front"],
            **body,
        }
        return await self.client.post(f"{API}/schedules", headers=person.headers, json=payload)

    async def schedule(self, person: Person, **body: Any) -> dict[str, Any]:
        """:meth:`create`; asserts 201."""
        response = await self.create(person, **body)
        assert response.status_code == 201, response.text
        result: dict[str, Any] = response.json()
        return result

    async def runs(self, person: Person, schedule_id: str) -> list[dict[str, Any]]:
        """The run records of a schedule, newest first."""
        response = await self.client.get(
            f"{API}/schedules/{schedule_id}/runs", headers=person.headers
        )
        assert response.status_code == 200, response.text
        items: list[dict[str, Any]] = response.json()["items"]
        return items

    async def state(
        self,
        mower_id: UUID,
        account_id: UUID,
        activity: MowerActivity,
        *,
        at: datetime | None = None,
        position: tuple[float, float] | None = None,
    ) -> None:
        """Publish a state like the Device-Gateway."""
        state = MowerState(
            mower_id=mower_id,
            account_id=account_id,
            observed_at=at or self.clock.now,
            activity=activity,
            battery_percent=80,
            position=GeoPosition(lat=position[0], lon=position[1]) if position else None,
            connection=ConnectionKind.SIMULATED,
        )
        await self.bus.publish(telemetry_subject(mower_id), state)

    async def map(
        self,
        mower_id: UUID,
        account_id: UUID,
        *,
        areas: Iterable[str] = ("front", "back"),
        origin: tuple[float, float] = (48.137154, 11.576124),
    ) -> None:
        """Publish a map snapshot like the Device-Gateway."""
        square = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)]
        snapshot = MowerMapSnapshot(
            mower_id=mower_id,
            account_id=account_id,
            observed_at=self.clock.now,
            map=MowerMap(
                reference=GeoReference(origin_lat=origin[0], origin_lon=origin[1]),
                areas=[MapArea(id=a, boundary=square) for a in areas],
            ),
        )
        await self.bus.publish(device_subject(mower_id, "map"), snapshot)

    async def capabilities(self, mower_id: UUID, account_id: UUID, **values: Any) -> None:
        """Publish a capability snapshot like the Device-Gateway."""
        snapshot = MowerCapabilitiesSnapshot(
            mower_id=mower_id,
            account_id=account_id,
            observed_at=self.clock.now,
            capabilities=MowerCapabilities.model_validate(values),
        )
        await self.bus.publish(device_subject(mower_id, "capabilities"), snapshot)

    async def tick(self) -> int:
        """One tick of the runner."""
        return await self.services.runner.tick()

    def events(self, event_type: str) -> list[dict[str, Any]]:
        """Payloads of the Scheduler's events of one type."""
        found = []
        for message in self.bus.messages("event.scheduler.>"):
            envelope = message.model(EventEnvelope[dict[str, Any]])
            if envelope.type == event_type:
                found.append(envelope.payload)
        return found

    async def fleet_command(self, owner: Person, command_id: str) -> dict[str, Any]:
        """Fleet's record of a command."""
        response = await self.fleet_client.get(
            f"{FLEET}/commands/{command_id}", headers=owner.headers
        )
        assert response.status_code == 200, response.text
        result: dict[str, Any] = response.json()
        return result


async def fake_gateway(bus: InMemoryBus, sink: list[CommandEnvelope]) -> None:
    """Answer every command like the Device-Gateway: ``accepted``, then ``executed``."""

    async def handle(message: BusMessage) -> None:
        envelope = message.model(CommandEnvelope)
        sink.append(envelope)
        for status in (CommandStatus.ACCEPTED, CommandStatus.EXECUTED):
            await publish_command_result(
                bus,
                CommandResult(
                    command_id=envelope.command_id,
                    mower_id=envelope.mower_id,
                    status=status,
                    correlation_id=envelope.correlation_id,
                ),
            )

    await bus.subscribe(ALL_COMMANDS, handle, durable="device-gateway-0")


@asynccontextmanager
async def harness(
    *,
    store: Any = None,
    bus: Any = None,
    with_fleet: bool = True,
    background_tasks: bool = False,
    fleet: Any = None,
    weather: Any = None,
    entitlements: Any = None,
    **overrides: Any,
) -> AsyncIterator[Harness]:
    """Both apps with fakes, inside their lifespans."""
    memory = store or MemoryStore()
    fleet_store = FleetStore()
    authz = InMemoryAuthorizer()
    active_bus = bus or InMemoryBus()
    keys = TestKeys()
    clock = ManualClock()
    provider = weather or StaticWeatherProvider("dry", clock=clock)
    app = build_app(
        make_settings(**overrides),
        bus=active_bus,
        store=memory,
        authorizer=authz,
        principal_resolver=keys.resolver(),
        clock=clock,
        weather=provider,
        fleet=fleet,
        background_tasks=background_tasks,
        entitlements=entitlements,
    )
    fleet_app = build_fleet(
        FleetSettings.model_validate(
            {"environment": "test", "log_format": "console", "nats_enabled": False}
        ),
        bus=active_bus,
        store=fleet_store,
        authorizer=authz,
        tuple_backend=authz,
        principal_resolver=keys.resolver(),
        background_tasks=False,
    )
    async with AsyncExitStack() as stack:
        fleet_client = await stack.enter_async_context(lifespan_client(fleet_app))
        if not with_fleet:
            for subscription in fleet_app.state.subscriptions:
                await subscription.unsubscribe()
        client = await stack.enter_async_context(lifespan_client(app))
        h = Harness(
            client,
            fleet_client,
            memory,
            fleet_store,
            authz,
            active_bus,
            keys,
            app,
            fleet_app,
            clock,
            provider,
        )
        await fake_gateway(active_bus, h.commands)
        yield h
