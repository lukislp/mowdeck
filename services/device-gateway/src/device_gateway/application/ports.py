"""Ports: what the gateway's use cases need from the outside world, as protocols."""

from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from mowdeck_contracts.v1 import (
    CommandResult,
    DiscoveredDevice,
    MowerCapabilitiesSnapshot,
    MowerConnectionEvent,
    MowerMapSnapshot,
    MowerPathSnapshot,
    MowerRegistration,
    MowerState,
    ProviderDescriptor,
)
from mowdeck_contracts.v1.relay import CameraSide, VisionDemand
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
    """No frame of the live camera; the message is safe to return (never a token)."""


class SnapshotUnavailable(SnapshotFailed):
    """This frame source cannot serve the mower at all (not reachable, switched off, no driver
    for the camera's provider): the next configured source may."""


@dataclass(frozen=True, slots=True)
class SnapshotTarget:
    """What a frame is wanted of: the gateway's own connection, never a caller's input."""

    account_id: UUID
    mower_id: UUID
    demand: VisionDemand = VisionDemand.ROUTINE
    """Why the photo is wanted (``PhotoRequest.demand``): the relay lets ``error_analysis``
    wake a sleeping mower and skips the Vision budget for it."""


class CameraSnapshotter(Protocol):
    """Still frames of a mower's live camera, for adapters that offer a camera but cannot take
    a photo themselves (no ``PhotoCapture``), such as the Mammotion cloud connection.

    The implementation is the camera relay's frame tap (ADR 0045, ``RelaySnapshotter``: a
    frame of the session the relay holds or starts, never a second vendor session while someone
    watches). The capture worker of ADR 0035 that joined the vendor channel itself is removed.
    """

    async def snapshot(self, target: SnapshotTarget, *, timeout_seconds: float) -> Photo:
        """One JPEG; raises :class:`SnapshotUnavailable` (try the next source) or
        :class:`SnapshotFailed`."""
        ...


@dataclass(frozen=True, slots=True)
class CameraFrame:
    """One camera's still of a moment and what is known about it."""

    photo: Photo
    captured_at: datetime | None = None
    """When the camera took it, as well as the source knows."""
    camera_id: str | None = None
    camera_name: str | None = None
    side: CameraSide | None = None
    """``left`` or ``right`` for a front camera of a stereo pair."""


@dataclass(frozen=True, slots=True)
class CameraFrames:
    """Stills of the same moment, left before right, and the cameras that gave none."""

    frames: tuple[CameraFrame, ...]
    missing: tuple[str, ...] = ()


@runtime_checkable
class FrameSetSnapshotter(Protocol):
    """A :class:`CameraSnapshotter` that also takes one frame of each camera of the same moment
    (the camera relay's frame set: both front cameras as a stereo pair)."""

    async def frame_set(
        self,
        target: SnapshotTarget,
        *,
        max_frames: int,
        max_bytes: int,
        timeout_seconds: float,
    ) -> CameraFrames:
        """Up to ``max_frames`` frames, each at most ``max_bytes``; one silent camera is left
        out (``missing``), never failing the set. Raises like ``snapshot``."""
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

    async def path_snapshot(self, snapshot: MowerPathSnapshot, fingerprint: str) -> None:
        """Publish the mow path of the running job on ``device.mower.<id>.path``."""
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
    """The enabled cloud providers (ADR 0065): their descriptors, and a login to one vendor
    account (or a reuse of its open session) that lists the account's devices."""

    def descriptors(self) -> list[ProviderDescriptor]:
        """The enabled providers, sorted by display name."""
        ...

    def descriptor(self, provider: str) -> ProviderDescriptor | None:
        """The descriptor of ``provider``; None when it is not installed or not enabled."""
        ...

    async def discover(self, provider: str, credentials: Mapping[str, str]) -> CloudDevices:
        """The devices; raises :class:`CloudLoginError` or :class:`CloudUnavailableError`."""
        ...


@dataclass(frozen=True, slots=True)
class MowerCredentials:
    """The secrets Fleet withheld from a registration: the connection's options and those of
    every additional link by kind (ADR 0038). Never logged."""

    options: dict[str, str] = field(default_factory=dict)
    link_secrets: dict[str, dict[str, str]] = field(default_factory=dict)


class CredentialSource(Protocol):
    """Fetches the secrets Fleet withholds from registrations (``fleet.mowers.credentials``)."""

    async def fetch(self, registration: MowerRegistration) -> MowerCredentials:
        """The withheld options of the mower and its links; raises when Fleet has none or does
        not answer. Callers keep the values only in the connection they open and never log
        them."""
        ...


class NoCredentials:
    """Without Fleet (tests, bootstrap mowers): no secrets can be fetched."""

    async def fetch(self, registration: MowerRegistration) -> MowerCredentials:
        """Always refused."""
        msg = "no credential source is configured"
        raise LookupError(msg)
