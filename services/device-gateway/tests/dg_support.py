"""Helpers for the device-gateway tests (a plain module: the repository has no conftest.py)."""

import asyncio
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI
from mowdeck_contracts.v1 import (
    CommandEnvelope,
    CommandResult,
    ConnectionKind,
    EventEnvelope,
    MowerCommand,
    MowerConnectionEvent,
    MowerRegistration,
    MowerState,
    PrincipalKind,
    PrincipalRef,
)
from mowdeck_contracts.v1.subjects import command_result_subject, telemetry_subject
from mowdeck_control import (
    Dispatch,
    JobCommand,
    MowerConnection,
    SimulatedMower,
    SimulationConfig,
)
from mowdeck_service_kit import PrincipalResolver
from mowdeck_service_kit.bus import InMemoryBus

from device_gateway.adapters.control.connections import RegistryConnectionProvider
from device_gateway.application.ports import BackgroundJob, FleetDirectory
from device_gateway.main import build_app
from device_gateway.settings import Settings

ISSUER = PrincipalRef(subject=UUID(int=99), kind=PrincipalKind.USER)


def make_settings(**overrides: Any) -> Settings:
    """Fast timings, no NATS, independent of the environment."""
    values: dict[str, Any] = {
        "environment": "test",
        "log_format": "console",
        "telemetry_heartbeat_seconds": 5.0,
        "sim_time_scale": 60.0,
        "sim_tick_seconds": 0.05,
        "connection_check_seconds": 0.02,
        "reconnect_initial_seconds": 0.05,
        "reconnect_max_seconds": 0.1,
        "fleet_request_timeout_seconds": 0.05,
        "fleet_retry_max_seconds": 1.0,
        "shutdown_grace_seconds": 5.0,
        **overrides,
    }
    return Settings.model_validate(values)


def mower_id_in_shard(index: int, count: int) -> UUID:
    """A random mower id that belongs to shard ``index`` of ``count``."""
    while True:
        candidate = uuid4()
        if candidate.int % count == index:
            return candidate


def registration(
    mower_id: UUID | None = None,
    *,
    account_id: UUID | None = None,
    name: str = "Test mower",
    options: dict[str, Any] | None = None,
) -> MowerRegistration:
    """A simulated mower registration."""
    return MowerRegistration(
        mower_id=mower_id or uuid4(),
        account_id=account_id or uuid4(),
        name=name,
        connection_kind=ConnectionKind.SIMULATED,
        connection_options=options or {},
    )


def envelope(
    reg: MowerRegistration,
    command: MowerCommand,
    *,
    account_id: UUID | None = None,
    ttl: timedelta = timedelta(seconds=30),
    now: datetime | None = None,
) -> CommandEnvelope:
    """A command for ``reg`` issued by its owner (or by ``account_id``)."""
    return CommandEnvelope.create(
        mower_id=reg.mower_id,
        account_id=account_id or reg.account_id,
        issued_by=ISSUER,
        command=command,
        ttl=ttl,
        now=now,
    )


class SlowMower(SimulatedMower):
    """A simulated mower whose non-emergency commands take ``delay`` seconds.

    ``executed`` records every command that reached the mower.
    """

    def __init__(self, *, delay: float = 0.0, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.delay = delay
        self.executed: list[str] = []

    async def dispatch(self, command: JobCommand) -> Dispatch:
        # The controller sends through ``dispatch`` (``execute`` is built on it).
        self.executed.append(command.kind)
        if command.kind != "emergency_stop" and self.delay:
            await asyncio.sleep(self.delay)
        return await super().dispatch(command)


class FailingConnection(SimulatedMower):
    """A connection that never connects."""

    async def connect(self) -> None:
        raise ConnectionError("no route to mower")


ConnectionFactory = Callable[[MowerRegistration], MowerConnection]


class TestProvider:
    """Connection provider for tests: per-mower factories, records every connection."""

    __test__ = False  # not a pytest test class

    def __init__(
        self,
        factories: dict[UUID, ConnectionFactory] | None = None,
        *,
        tick_seconds: float | None = 0.05,
    ) -> None:
        self.factories = factories or {}
        self.created: dict[UUID, list[MowerConnection]] = {}
        self._tick = tick_seconds
        self._registry = RegistryConnectionProvider(sim_tick_seconds=tick_seconds or 1.0)

    def create(self, registration: MowerRegistration) -> MowerConnection:
        factory = self.factories.get(registration.mower_id)
        connection: MowerConnection = (
            factory(registration)
            if factory is not None
            else SlowMower(
                mower_id=registration.mower_id,
                account_id=registration.account_id,
                config=SimulationConfig(time_scale=60),
            )
        )
        self.created.setdefault(registration.mower_id, []).append(connection)
        return connection

    def latest(self, mower_id: UUID) -> MowerConnection:
        return self.created[mower_id][-1]

    def background_jobs(self, connection: MowerConnection) -> Sequence[BackgroundJob]:
        if self._tick is None:
            return []
        return self._registry.background_jobs(connection)


class SilentFleet:
    """A Fleet that never answers (the request times out)."""

    def __init__(self) -> None:
        self.calls = 0

    async def list_mowers(self, shard: object) -> list[MowerRegistration]:
        self.calls += 1
        raise TimeoutError("fleet.mowers.list timed out")


def make_app(
    *,
    bus: InMemoryBus,
    provider: TestProvider | None = None,
    principal_resolver: PrincipalResolver | None = None,
    fleet: FleetDirectory | None = None,
    **overrides: Any,
) -> FastAPI:
    """The gateway app with an in-memory bus and a test provider."""
    return build_app(
        make_settings(**overrides),
        bus=bus,
        principal_resolver=principal_resolver,
        connection_provider=provider or TestProvider(),
        fleet=fleet or SilentFleet(),
    )


async def wait_until(
    predicate: Callable[[], object], *, within: float = 3.0, interval: float = 0.01
) -> None:
    """Poll ``predicate`` until it is truthy; fail after ``within`` seconds."""
    deadline = time.monotonic() + within
    while not predicate():
        if time.monotonic() > deadline:
            msg = "condition not met in time"
            raise AssertionError(msg)
        await asyncio.sleep(interval)


def results(bus: InMemoryBus, mower_id: UUID) -> list[CommandResult]:
    """Command results published for ``mower_id``."""
    return [m.model(CommandResult) for m in bus.messages(command_result_subject(mower_id))]


def result_for(bus: InMemoryBus, command: CommandEnvelope) -> CommandResult | None:
    """The first result of ``command``, if published."""
    found = [r for r in results(bus, command.mower_id) if r.command_id == command.command_id]
    return found[0] if found else None


def telemetry(bus: InMemoryBus, mower_id: UUID) -> list[MowerState]:
    """Telemetry snapshots published for ``mower_id``."""
    return [m.model(MowerState) for m in bus.messages(telemetry_subject(mower_id))]


def connection_events(bus: InMemoryBus, event_type: str) -> list[MowerConnectionEvent]:
    """Payloads of ``mower.connected`` or ``mower.disconnected`` events."""
    subject = f"event.{event_type}"
    return [m.model(EventEnvelope[MowerConnectionEvent]).payload for m in bus.messages(subject)]


class SilentPublisher:
    """A publisher that drops everything (gateway tests without a bus)."""

    async def command_result(self, result: object) -> None:
        return None

    async def telemetry(self, state: object) -> None:
        return None

    async def connection_event(self, event_type: str, payload: object) -> None:
        return None

    async def map_snapshot(self, snapshot: object, fingerprint: str) -> None:
        return None

    async def capabilities_snapshot(self, snapshot: object, fingerprint: str) -> None:
        return None
