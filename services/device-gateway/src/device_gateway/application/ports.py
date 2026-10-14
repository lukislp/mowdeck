"""Ports: what the gateway's use cases need from the outside world, as protocols."""

from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from mowdeck_contracts.v1 import (
    CameraSession,
    CloudProvider,
    CommandResult,
    DiscoveredDevice,
    MowerCapabilitiesSnapshot,
    MowerConnectionEvent,
    MowerMapSnapshot,
    MowerRegistration,
    MowerState,
)
from mowdeck_control import MowerConnection, Photo

from device_gateway.domain.rules import Shard

BackgroundJob = Callable[[], Coroutine[Any, Any, None]]
"""A coroutine factory that runs as long as a connection is up (e.g. simulation ticks)."""


class Clock(Protocol):
    """Returns the current time (timezone-aware)."""

    def __call__(self) -> datetime:
        """The current time."""
        ...


class SnapshotFailed(Exception):  # noqa: N818 - reads as an outcome
    """The capture worker gave no frame; the message is safe to return (never the token)."""


class CameraSnapshotter(Protocol):
    """Still frames of a live camera session (the capture worker, ADR 0035).

    Used for adapters that offer a camera session (``CameraCapable``) but cannot take a photo
    themselves (no ``PhotoCapture``), such as the Mammotion cloud connection.
    """

    async def snapshot(
        self, session: CameraSession, camera_uid: int | None, *, timeout_seconds: float
    ) -> Photo:
        """One JPEG of the camera ``camera_uid``; raises :class:`SnapshotFailed`."""
        ...


class GatewayPublisher(Protocol):
    """Everything the gateway publishes on the bus."""

    async def command_result(self, result: CommandResult) -> None:
        """Publish the outcome of a command on ``command.result.mower.<id>``."""
        ...

    async def telemetry(self, state: MowerState) -> None:
        """Publish a state snapshot on ``telemetry.mower.<id>``."""
        ...

    async def connection_event(self, event_type: str, payload: MowerConnectionEvent) -> None:
        """Publish ``mower.connected`` or ``mower.disconnected``."""
        ...

    async def map_snapshot(self, snapshot: MowerMapSnapshot, fingerprint: str) -> None:
        """Publish the map on ``device.mower.<id>.map`` (last value per mower)."""
        ...

    async def capabilities_snapshot(
        self, snapshot: MowerCapabilitiesSnapshot, fingerprint: str
    ) -> None:
        """Publish the capabilities on ``device.mower.<id>.capabilities``."""
        ...


class FleetDirectory(Protocol):
    """Source of the registrations of this gateway's shard (Fleet)."""

    async def list_mowers(self, shard: Shard) -> list[MowerRegistration]:
        """Return the registrations of ``shard``; raise when Fleet does not answer."""
        ...


class ConnectionProvider(Protocol):
    """Creates mower connections (through the ``mowdeck-control`` registry)."""

    def create(self, registration: MowerRegistration) -> MowerConnection:
        """Create a new, not yet connected link for ``registration``."""
        ...

    def background_jobs(self, connection: MowerConnection) -> Sequence[BackgroundJob]:
        """Jobs that must run while ``connection`` is up (the simulator's clock, for example)."""
        ...


class CloudLoginError(Exception):
    """The vendor cloud refused the credentials of a discovery (never holds them)."""


class CloudUnavailableError(Exception):
    """The vendor cloud did not answer a discovery or failed; the message is safe to show."""


@dataclass(frozen=True, slots=True)
class CloudDevices:
    """The devices of a vendor cloud account and whether an open session answered."""

    devices: list[DiscoveredDevice]
    reused_session: bool


class CloudAccounts(Protocol):
    """Logs in to a vendor cloud account (or reuses its open session) and lists its devices."""

    def supports(self, provider: CloudProvider) -> bool:
        """True when an adapter for ``provider`` is installed."""
        ...

    async def discover(self, provider: CloudProvider, email: str, password: str) -> CloudDevices:
        """The devices; raises :class:`CloudLoginError` or :class:`CloudUnavailableError`."""
        ...


class CredentialSource(Protocol):
    """Fetches the secrets Fleet withholds from registrations (``fleet.mowers.credentials``)."""

    async def fetch(self, registration: MowerRegistration) -> dict[str, str]:
        """The withheld options of the mower; raises when Fleet has none or does not answer.
        Callers keep the values only in the connection they open and never log them."""
        ...


class NoCredentials:
    """Without Fleet (tests, bootstrap mowers): no secrets can be fetched."""

    async def fetch(self, registration: MowerRegistration) -> dict[str, str]:
        """Always refused."""
        msg = "no credential source is configured"
        raise LookupError(msg)
