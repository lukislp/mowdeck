"""Helpers for the telemetry tests (a plain module: the repository has no conftest.py).

``harness()`` builds the real app with the memory store, the in-memory OpenFGA fake (it
evaluates the packaged model), the in-memory bus and access tokens minted with
``mowdeck_auth.testing.TestKeys``. Fleet's part (the mower tuples and the ``mower.registered``
event) is written the way Fleet writes it, so the consumers and the routes are tested together.
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
from mowdeck_authz import RelationshipTuple, TupleWriter, account, mower, principal_subject
from mowdeck_authz.testing import InMemoryAuthorizer
from mowdeck_contracts.v1 import (
    MOWER_REGISTERED,
    MOWER_REMOVED,
    CommandResult,
    CommandStatus,
    ConnectionKind,
    EventEnvelope,
    GeoPosition,
    MowerActivity,
    MowerRegistration,
    MowerState,
    Principal,
    PrincipalKind,
    RtkFixStatus,
)
from mowdeck_contracts.v1.subjects import telemetry_subject
from mowdeck_service_kit.bus import InMemoryBus, publish_command_result, publish_event
from mowdeck_service_kit.testing import lifespan_client

from telemetry.adapters.memory import MemoryStore
from telemetry.main import build_app
from telemetry.settings import Settings

API = "/api/v1/telemetry"
T0 = datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC)


def make_settings(**overrides: Any) -> Settings:
    """Settings for tests (environment independent)."""
    values: dict[str, Any] = {
        "environment": "test",
        "log_format": "console",
        "nats_enabled": False,
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
    rtk_fix: RtkFixStatus = RtkFixStatus.FIXED,
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
        rtk_fix=rtk_fix,
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


@dataclass
class Harness:
    """The app under test and its fakes."""

    client: httpx.AsyncClient
    store: MemoryStore
    authz: InMemoryAuthorizer
    bus: InMemoryBus
    keys: TestKeys
    app: FastAPI
    fleet: TupleWriter = field(init=False)
    identity: TupleWriter = field(init=False)

    def __post_init__(self) -> None:
        self.fleet = TupleWriter(self.authz, owner="fleet")
        self.identity = TupleWriter(self.authz, owner="identity")

    async def owner(self) -> Person:
        """A new account and its owner (``account#owner``, ``account#member``)."""
        principal = Principal(subject=uuid4(), account_id=uuid4(), kind=PrincipalKind.USER)
        subject = principal_subject(principal)
        assert principal.account_id is not None
        await self.identity.write(
            [
                RelationshipTuple(subject, "owner", account(principal.account_id)),
                RelationshipTuple(subject, "member", account(principal.account_id)),
            ]
        )
        return Person(principal, self.keys.mint(principal))

    async def add_mower(self, owner: Person, *, announce: bool = True) -> UUID:
        """Register a mower like Fleet does: the tuple, then ``mower.registered``."""
        mower_id = uuid4()
        await self.fleet.write(
            [RelationshipTuple(account(owner.account_id).as_subject(), "account", mower(mower_id))]
        )
        if announce:
            await self.announce(mower_id, owner.account_id)
        return mower_id

    async def announce(
        self,
        mower_id: UUID,
        account_id: UUID,
        *,
        removed: bool = False,
        options: dict[str, Any] | None = None,
    ) -> None:
        """Publish ``mower.registered`` (or ``mower.removed``)."""
        registration = MowerRegistration(
            mower_id=mower_id,
            account_id=account_id,
            name="Front lawn",
            connection_kind=ConnectionKind.SIMULATED,
            connection_options=options or {},
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

    async def publish_state(self, state: MowerState) -> None:
        """Publish like the Device-Gateway (no msg id: heartbeats repeat on purpose)."""
        await self.bus.publish(telemetry_subject(state.mower_id), state)

    async def publish_result(
        self,
        mower_id: UUID,
        status: CommandStatus,
        *,
        command_id: UUID | None = None,
        at: datetime = T0,
    ) -> UUID:
        """Publish a command result; returns the command id."""
        command = command_id or uuid4()
        await publish_command_result(
            self.bus, CommandResult(command_id=command, mower_id=mower_id, status=status, at=at)
        )
        return command


@asynccontextmanager
async def harness(
    *,
    store: Any = None,
    bus: Any = None,
    authz: InMemoryAuthorizer | None = None,
    background_tasks: bool = False,
    clock: Any = None,
    tracking_gateway: Any = None,
    aerial_source: Any = None,
    aerial_cache: Any = None,
    zone_store: Any = None,
    tracking_store: Any = None,
    **overrides: Any,
) -> AsyncIterator[Harness]:
    """The app with fakes, inside its lifespan."""
    memory = store or MemoryStore()
    fake = authz or InMemoryAuthorizer()
    active_bus = bus or InMemoryBus()
    keys = TestKeys()
    app = build_app(
        make_settings(**overrides),
        bus=active_bus,
        store=memory,
        authorizer=fake,
        principal_resolver=keys.resolver(),
        background_tasks=background_tasks,
        clock=clock,
        tracking_gateway=tracking_gateway,
        zone_store=zone_store,
        tracking_store=tracking_store,
        seed_delay_seconds=0.01,
        aerial_source=aerial_source,
        aerial_cache=aerial_cache,
    )
    async with lifespan_client(app) as client:
        yield Harness(client, memory, fake, active_bus, keys, app)
