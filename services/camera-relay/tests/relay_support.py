"""Helpers of the camera-relay tests: a fake clock, fake ports and builders."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from mowdeck_contracts.v1 import EventEnvelope, PrincipalKind
from mowdeck_contracts.v1.camera import CameraInfo, CameraProvider
from mowdeck_contracts.v1.principal import PrincipalRef
from mowdeck_contracts.v1.relay import RelayAttachRequest, RelayPurpose

from camera_relay.adapters.memory import InMemoryRelayRepository
from camera_relay.adapters.tickets import Ed25519TicketSigner
from camera_relay.application.ports import SourceError, SourceOffer
from camera_relay.application.service import RelayService
from camera_relay.domain.effects import (
    Effect,
    EmitEvent,
    StartVendorSession,
    StopVendorSession,
    UpdateCameras,
)
from camera_relay.domain.models import RelayKey
from camera_relay.domain.policy import MowerStatus
from camera_relay.domain.session import RelaySession

T0 = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
ONLINE = MowerStatus(online=True, battery_percent=80, quota_remaining_percent=90)


class FakeClock:
    """A clock that only moves when told to."""

    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> datetime:
        self.now += timedelta(seconds=seconds)
        return self.now


def at(seconds: float) -> datetime:
    """``T0`` plus ``seconds``."""
    return T0 + timedelta(seconds=seconds)


OWNER_SUBJECT = UUID("00000000-0000-4000-8000-0000000000a1")
"""The user every helper request comes from (leases belong to their principal)."""


def principal(subject: UUID | None = None) -> PrincipalRef:
    """A user principal: the helpers' owner unless ``subject`` names another user."""
    return PrincipalRef(subject=subject or OWNER_SUBJECT, kind=PrincipalKind.USER)


def key(account_id: UUID | None = None, mower_id: UUID | None = None) -> RelayKey:
    return RelayKey(account_id or uuid4(), mower_id or uuid4())


def session(status: MowerStatus | None = ONLINE, relay_key: RelayKey | None = None) -> RelaySession:
    return RelaySession(relay_key or key(), status=status)


def of_type[T](effects: list[Effect], kind: type[T]) -> list[T]:
    return [e for e in effects if isinstance(e, kind)]


def events(effects: list[Effect], kind: str | None = None) -> list[EmitEvent]:
    return [e for e in of_type(effects, EmitEvent) if kind is None or e.kind == kind]


def attach_request(
    relay_key: RelayKey,
    cameras: list[str] | None = None,
    purpose: RelayPurpose = RelayPurpose.VIEWER,
) -> RelayAttachRequest:
    return RelayAttachRequest(
        account_id=relay_key.account_id,
        mower_id=relay_key.mower_id,
        camera_ids=cameras or ["front_left"],
        requested_by=principal(),
        purpose=purpose,
    )


def offered_cameras(*ids: str) -> tuple[CameraInfo, ...]:
    return tuple(CameraInfo(id=camera, name=camera.replace("_", " ")) for camera in ids)


@dataclass
class FakeVendor:
    """Records the source commands; ``fail_with`` makes every start fail, ``offer_fails_with``
    every offer. ``cameras`` is what the vendor session offers (empty: no camera list)."""

    fail_with: SourceError | None = None
    offer_fails_with: SourceError | None = None
    provider: CameraProvider = CameraProvider.FAKE
    cameras: tuple[CameraInfo, ...] = ()
    offers: list[RelayKey] = field(default_factory=list)
    discarded: list[RelayKey] = field(default_factory=list)
    starts: list[StartVendorSession] = field(default_factory=list)
    updates: list[UpdateCameras] = field(default_factory=list)
    stops: list[StopVendorSession] = field(default_factory=list)

    async def offer(self, key: RelayKey) -> SourceOffer:
        self.offers.append(key)
        if self.offer_fails_with is not None:
            raise self.offer_fails_with
        return SourceOffer(provider=self.provider, cameras=self.cameras)

    async def discard(self, key: RelayKey) -> None:
        self.discarded.append(key)

    async def start(self, command: StartVendorSession) -> None:
        self.starts.append(command)
        if self.fail_with is not None:
            raise self.fail_with

    async def update_cameras(self, command: UpdateCameras) -> None:
        self.updates.append(command)

    async def stop(self, command: StopVendorSession) -> None:
        self.stops.append(command)


@dataclass
class FakeMedia:
    kicked: list[str] = field(default_factory=list)

    async def kick(self, whep_session: str) -> None:
        self.kicked.append(whep_session)


@dataclass
class RecordingPublisher:
    published: list[tuple[str, EventEnvelope[Any]]] = field(default_factory=list)

    async def publish(self, subject: str, envelope: EventEnvelope[Any]) -> None:
        self.published.append((subject, envelope))

    def types(self) -> list[str]:
        return [envelope.type for _, envelope in self.published]


@dataclass
class Harness:
    """A ``RelayService`` with fake ports and a fake clock."""

    clock: FakeClock = field(default_factory=FakeClock)
    vendor: FakeVendor = field(default_factory=FakeVendor)
    media: FakeMedia = field(default_factory=FakeMedia)
    events: RecordingPublisher = field(default_factory=RecordingPublisher)
    signer: Ed25519TicketSigner = field(default_factory=Ed25519TicketSigner.generate)
    repository: InMemoryRelayRepository = field(default_factory=InMemoryRelayRepository)
    service: RelayService = field(init=False)

    def __post_init__(self) -> None:
        self.service = RelayService(
            repository=self.repository,
            vendor=self.vendor,
            media=self.media,
            events=self.events,
            signer=self.signer,
            clock=self.clock,
        )
