"""Helpers for the notifications tests (a plain module: the repository has no conftest.py).

``harness()`` builds the real app with the memory store, the in-memory OpenFGA fake (it
evaluates the packaged model), the in-memory bus, a manual clock, a recording channel and access
tokens minted with ``mowdeck_auth.testing.TestKeys``. Fleet's part (the mower tuples and the
``mower.registered`` event) is written the way Fleet writes it, so consumers, rules, fan-out and
routes are tested together.
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
from mowdeck_authz import (
    RelationshipTuple,
    TupleWriter,
    account,
    mower,
    principal_subject,
    user,
)
from mowdeck_authz.testing import InMemoryAuthorizer
from mowdeck_contracts.v1 import (
    MOWER_CONNECTED,
    MOWER_DISCONNECTED,
    MOWER_REGISTERED,
    MOWER_REMOVED,
    ConnectionKind,
    EventEnvelope,
    GeoPosition,
    MowerActivity,
    MowerConnectionEvent,
    MowerRegistration,
    MowerState,
    Principal,
    PrincipalKind,
)
from mowdeck_contracts.v1.subjects import telemetry_subject
from mowdeck_service_kit.bus import InMemoryBus, publish_event
from mowdeck_service_kit.testing import lifespan_client

from notifications.adapters.channels import ChannelRegistry
from notifications.adapters.memory import MemoryStore
from notifications.application.services import Services
from notifications.domain.models import Delivery, DeliveryOutcome, DeliveryResult
from notifications.main import build_app
from notifications.settings import Settings

API = "/api/v1/notifications"
T0 = datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC)

# A valid subscriber key pair for Web Push tests (P-256 public point and 16 byte auth secret).
P256DH = "BNcRdreALRFXTkOOUHK1EtK2wtaz5Ry4YfYCA_0QTpQtUbVlUls0VJXg7A8u-Ts1XbjhazAkj7I99e8QcYP7DkM"
AUTH = "tBHItJI5svbpez7KI4CCXg"


class ManualClock:
    """A clock the test moves forward."""

    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> datetime:
        """Move forward and return the new time."""
        self.now += timedelta(seconds=seconds)
        return self.now


class RecordingChannel:
    """A channel that records deliveries and answers with scripted results."""

    def __init__(self, name: str = "log", results: list[DeliveryResult] | None = None) -> None:
        self.name = name
        self.sent: list[Delivery] = []
        self.results = list(results or [])
        self.error: Exception | None = None

    async def send(self, delivery: Delivery) -> DeliveryResult:
        self.sent.append(delivery)
        if self.error is not None:
            raise self.error
        if self.results:
            return self.results.pop(0)
        return DeliveryResult(DeliveryOutcome.SENT)


def make_settings(**overrides: Any) -> Settings:
    """Settings for tests (environment independent)."""
    values: dict[str, Any] = {
        "environment": "test",
        "log_format": "console",
        "nats_enabled": False,
        "notifications_channels": "log",
        "notifications_ntfy_allowed_servers": "https://ntfy.example.test",
        "notifications_webpush_allowed_hosts": "push.example.test",
        **overrides,
    }
    return Settings.model_validate(values)


def make_state(
    mower_id: UUID,
    account_id: UUID,
    *,
    at: datetime = T0,
    activity: MowerActivity = MowerActivity.DOCKED,
    battery: int = 90,
    position: tuple[float, float] | None = None,
    **extra: Any,
) -> MowerState:
    """A state of ``mower_id`` observed at ``at``."""
    return MowerState(
        mower_id=mower_id,
        account_id=account_id,
        observed_at=at,
        activity=activity,
        battery_percent=battery,
        position=GeoPosition(lat=position[0], lon=position[1]) if position else None,
        connection=ConnectionKind.SIMULATED,
        **extra,
    )


def seconds(n: float) -> timedelta:
    """Shortcut for ``timedelta(seconds=n)``."""
    return timedelta(seconds=n)


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

    @property
    def user_id(self) -> UUID:
        """The user id (``sub``)."""
        return self.principal.subject


@dataclass
class Harness:
    """The app under test and its fakes."""

    client: httpx.AsyncClient
    store: MemoryStore
    authz: InMemoryAuthorizer
    bus: InMemoryBus
    keys: TestKeys
    app: FastAPI
    clock: ManualClock
    channel: RecordingChannel
    fleet: TupleWriter = field(init=False)
    identity: TupleWriter = field(init=False)

    def __post_init__(self) -> None:
        self.fleet = TupleWriter(self.authz, owner="fleet")
        self.identity = TupleWriter(self.authz, owner="identity")

    @property
    def services(self) -> Services:
        """The wired use cases."""
        services: Services = self.app.state.notifications
        return services

    async def owner(
        self, *, perms: list[str] | None = None, account_id: UUID | None = None
    ) -> Person:
        """A new account (or ``account_id``) and its owner (``account#owner``,
        ``account#member``)."""
        principal = Principal(
            subject=uuid4(),
            account_id=account_id or uuid4(),
            kind=PrincipalKind.USER,
            permissions=perms if perms is not None else ["mower.view", "notifications.manage"],
        )
        subject = principal_subject(principal)
        assert principal.account_id is not None
        await self.identity.write(
            [
                RelationshipTuple(subject, "owner", account(principal.account_id)),
                RelationshipTuple(subject, "member", account(principal.account_id)),
            ]
        )
        return Person(principal, self.keys.mint(principal))

    async def member(self, owner: Person, *, perms: list[str] | None = None) -> Person:
        """A sub-user of ``owner``'s account without any grant yet."""
        principal = Principal(
            subject=uuid4(),
            account_id=owner.account_id,
            kind=PrincipalKind.SUBUSER,
            permissions=perms or [],
        )
        await self.identity.write(
            [RelationshipTuple(user(principal.subject), "member", account(owner.account_id))]
        )
        return Person(principal, self.keys.mint(principal))

    async def grant_view(self, person: Person, mower_id: UUID, *, revoke: bool = False) -> None:
        """Let ``person`` view one mower (``grant_mower_view``), or take it back."""
        item = RelationshipTuple(user(person.user_id), "grant_mower_view", mower(mower_id))
        if revoke:
            await self.identity.write(deletes=[item])
        else:
            await self.identity.write([item])

    async def add_mower(
        self,
        owner: Person,
        *,
        announce: bool = True,
        name: str = "Front lawn",
        mower_id: UUID | None = None,
    ) -> UUID:
        """Register a mower like Fleet does: the tuple, then ``mower.registered``."""
        mower_id = mower_id or uuid4()
        await self.fleet.write(
            [RelationshipTuple(account(owner.account_id).as_subject(), "account", mower(mower_id))]
        )
        if announce:
            await self.announce(mower_id, owner.account_id, name=name)
        return mower_id

    async def announce(
        self, mower_id: UUID, account_id: UUID, *, removed: bool = False, name: str = "Front lawn"
    ) -> None:
        """Publish ``mower.registered`` (or ``mower.removed``)."""
        registration = MowerRegistration(
            mower_id=mower_id,
            account_id=account_id,
            name=name,
            connection_kind=ConnectionKind.SIMULATED,
        )
        await publish_event(
            self.bus,
            EventEnvelope[MowerRegistration](
                type=MOWER_REMOVED if removed else MOWER_REGISTERED,
                producer="fleet",
                account_id=account_id,
                payload=registration,
            ),
        )

    async def link(self, mower_id: UUID, account_id: UUID, *, connected: bool) -> None:
        """Publish ``mower.connected`` or ``mower.disconnected`` like the Device-Gateway."""
        await publish_event(
            self.bus,
            EventEnvelope[MowerConnectionEvent](
                type=MOWER_CONNECTED if connected else MOWER_DISCONNECTED,
                producer="device-gateway",
                account_id=account_id,
                payload=MowerConnectionEvent(
                    mower_id=mower_id,
                    account_id=account_id,
                    connection_kind=ConnectionKind.SIMULATED,
                    at=self.clock.now,
                ),
            ),
        )

    async def publish_state(self, state: MowerState) -> None:
        """Publish like the Device-Gateway."""
        await self.bus.publish(telemetry_subject(state.mower_id), state)

    async def subscribe_all(self, person: Person, **extra: Any) -> httpx.Response:
        """``PUT /preferences``: every kind for every mower."""
        body = {
            "enabled_kinds": [
                "mowing_finished",
                "mower_stuck",
                "battery_low",
                "mower_offline",
                "mower_online",
                "job_started",
            ],
            "all_mowers": True,
            "channels": ["log", "webpush", "ntfy"],
            **extra,
        }
        response = await self.client.put(f"{API}/preferences", json=body, headers=person.headers)
        assert response.status_code == 200, response.text
        return response

    async def inbox(self, person: Person) -> list[dict[str, Any]]:
        """The person's inbox items."""
        response = await self.client.get(f"{API}/inbox", headers=person.headers)
        assert response.status_code == 200, response.text
        items: list[dict[str, Any]] = response.json()["items"]
        return items


@asynccontextmanager
async def harness(
    *,
    store: Any = None,
    bus: Any = None,
    authz: InMemoryAuthorizer | None = None,
    channels: dict[str, Any] | None = None,
    background_tasks: bool = False,
    entitlements: Any = None,
    **overrides: Any,
) -> AsyncIterator[Harness]:
    """The app with fakes, inside its lifespan."""
    memory = store or MemoryStore()
    fake = authz or InMemoryAuthorizer()
    active_bus = bus or InMemoryBus()
    keys = TestKeys()
    clock = ManualClock()
    channel = RecordingChannel()
    registry = ChannelRegistry(channels if channels is not None else {"log": channel})
    settings = make_settings(**overrides)
    app = build_app(
        settings,
        bus=active_bus,
        store=memory,
        authorizer=fake,
        principal_resolver=keys.resolver(),
        clock=clock,
        channels=registry,
        background_tasks=background_tasks,
        seed_delay_seconds=0.01,
        entitlements=entitlements,
    )
    async with lifespan_client(app) as client:
        yield Harness(client, memory, fake, active_bus, keys, app, clock, channel)
