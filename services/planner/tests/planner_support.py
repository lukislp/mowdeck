"""Helpers for the planner tests (a plain module: the repository has no conftest.py).

``harness()`` builds the real Planner app *and* the real Fleet app on one in-memory bus and one
in-memory OpenFGA fake (it evaluates the packaged model), with memory stores and access tokens
minted with ``mowdeck_auth.testing.TestKeys`` (both apps accept them). Mowers are registered in
Fleet (which writes their tuples); maps and capabilities are published like the Device-Gateway
does, a fake Telemetry answers ``tracks.positions.query`` from :attr:`Harness.track`, and a fake
gateway records every command.
"""

from collections.abc import AsyncIterator, Iterable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI
from mowdeck_auth.testing import TestKeys, bearer
from mowdeck_authz import ObjectRef, RelationshipTuple, TupleWriter, account, principal_subject
from mowdeck_authz.testing import InMemoryAuthorizer
from mowdeck_contracts.v1 import (
    TRACK_PRECISION,
    CommandEnvelope,
    MowerCapabilities,
    MowerCapabilitiesSnapshot,
    MowerMap,
    MowerMapSnapshot,
    PositionTrackReply,
    PositionTrackRequest,
    Principal,
    PrincipalKind,
    encode_polyline,
)
from mowdeck_contracts.v1.subjects import ALL_COMMANDS, TELEMETRY_POSITIONS_QUERY, device_subject
from mowdeck_control import SimulatedMower, default_map
from mowdeck_service_kit.bus import BusMessage, InMemoryBus
from mowdeck_service_kit.testing import lifespan_client

from fleet.adapters.memory import MemoryStore as FleetStore
from fleet.main import build_app as build_fleet
from fleet.settings import Settings as FleetSettings
from planner.adapters.memory import MemoryStore
from planner.main import build_app
from planner.settings import Settings

API = "/api/v1/planner"
FLEET = "/api/v1/fleet"
T0 = datetime(2026, 10, 10, 8, 0, tzinfo=UTC)

ROLE_KEYS: dict[str, tuple[str, ...]] = {
    "manager": ("mower.view", "mower.control", "schedule.edit", "fleet.manage"),
    "operator": ("mower.view", "mower.control", "mower.drive"),
    "viewer": ("mower.view",),
}

LUBA_LIKE = MowerCapabilities(
    model="LUBA-like",
    native_patterns=frozenset({"stripes", "diagonal", "checkerboard", "edge_only"}),
    custom_paths=False,
    max_speed_mps=1.0,
)
"""Capabilities of a mower without own routes."""


def sim_capabilities() -> MowerCapabilities:
    """The simulator's capabilities (``custom_paths`` is True)."""
    return SimulatedMower()._capabilities


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
    authz: InMemoryAuthorizer
    bus: InMemoryBus
    keys: TestKeys
    app: FastAPI
    identity: TupleWriter = field(init=False)
    commands: list[CommandEnvelope] = field(default_factory=list)
    track: list[tuple[float, float]] = field(default_factory=list)
    """``(lat, lon)`` points the fake Telemetry answers with."""
    track_requests: list[PositionTrackRequest] = field(default_factory=list)
    track_error: str | None = None

    def __post_init__(self) -> None:
        self.identity = TupleWriter(self.authz, owner="identity")

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

    async def register(self, owner: Person) -> UUID:
        """Register a simulated mower in Fleet; returns its id."""
        response = await self.fleet_client.post(
            f"{FLEET}/mowers",
            headers=owner.headers,
            json={"name": f"mower-{uuid4().hex[:6]}", "connection_kind": "simulated"},
        )
        assert response.status_code == 201, response.text
        return UUID(response.json()["id"])

    async def publish_map(
        self, mower_id: UUID, account_id: UUID, mower_map: MowerMap | None = None
    ) -> None:
        """Publish a map snapshot like the Device-Gateway (default: the simulator's map)."""
        snapshot = MowerMapSnapshot(
            mower_id=mower_id,
            account_id=account_id,
            observed_at=T0,
            map=mower_map or default_map(),
        )
        await self.bus.publish(device_subject(mower_id, "map"), snapshot)

    async def publish_capabilities(
        self, mower_id: UUID, account_id: UUID, capabilities: MowerCapabilities
    ) -> None:
        """Publish a capability snapshot like the Device-Gateway."""
        snapshot = MowerCapabilitiesSnapshot(
            mower_id=mower_id, account_id=account_id, observed_at=T0, capabilities=capabilities
        )
        await self.bus.publish(device_subject(mower_id, "capabilities"), snapshot)

    async def mower(self, owner: Person, capabilities: MowerCapabilities | None = None) -> UUID:
        """A registered mower with the simulator's map and the given capabilities."""
        mower_id = await self.register(owner)
        await self.publish_map(mower_id, owner.account_id)
        await self.publish_capabilities(
            mower_id, owner.account_id, capabilities or sim_capabilities()
        )
        return mower_id

    async def preview(self, person: Person, **body: Any) -> httpx.Response:
        """``POST /plans/preview``."""
        return await self.client.post(f"{API}/plans/preview", headers=person.headers, json=body)


async def _fake_gateway(h: Harness) -> None:
    async def handle(message: BusMessage) -> None:
        h.commands.append(message.model(CommandEnvelope))

    await h.bus.subscribe(ALL_COMMANDS, handle, durable="device-gateway-0")


async def _fake_telemetry(h: Harness) -> None:
    async def handle(message: BusMessage) -> None:
        request = message.model(PositionTrackRequest)
        h.track_requests.append(request)
        if h.track_error is not None:
            await message.respond(
                PositionTrackReply(mower_id=request.mower_id, error=h.track_error)
            )
            return
        await message.respond(
            PositionTrackReply(
                mower_id=request.mower_id,
                count=len(h.track),
                track=encode_polyline(h.track, TRACK_PRECISION),
            )
        )

    await h.bus.subscribe(TELEMETRY_POSITIONS_QUERY, handle, queue="telemetry-track")


@asynccontextmanager
async def harness(
    *,
    store: Any = None,
    bus: Any = None,
    entitlements: Any = None,
    telemetry: bool = True,
    **overrides: Any,
) -> AsyncIterator[Harness]:
    """Both apps with fakes, inside their lifespans."""
    memory = store or MemoryStore()
    authz = InMemoryAuthorizer()
    active_bus = bus or InMemoryBus()
    keys = TestKeys()
    app = build_app(
        make_settings(**overrides),
        bus=active_bus,
        store=memory,
        authorizer=authz,
        principal_resolver=keys.resolver(),
        entitlements=entitlements,
    )
    fleet_app = build_fleet(
        FleetSettings.model_validate(
            {"environment": "test", "log_format": "console", "nats_enabled": False}
        ),
        bus=active_bus,
        store=FleetStore(),
        authorizer=authz,
        tuple_backend=authz,
        principal_resolver=keys.resolver(),
        background_tasks=False,
    )
    async with AsyncExitStack() as stack:
        fleet_client = await stack.enter_async_context(lifespan_client(fleet_app))
        client = await stack.enter_async_context(lifespan_client(app))
        h = Harness(client, fleet_client, memory, authz, active_bus, keys, app)
        if isinstance(active_bus, InMemoryBus):
            await _fake_gateway(h)
            if telemetry:
                await _fake_telemetry(h)
        yield h
