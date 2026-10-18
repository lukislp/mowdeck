"""Ports: what the use cases need from the outside world, as protocols.

* :class:`VendorSessions` offers, starts, updates and stops the source of a session: the
  Device-Gateway's camera session and release requests and a source driver per
  ``CameraProvider`` (``adapters/gateway.py``); without a media server the service runs with an
  adapter that answers ``unavailable``.
* :class:`MediaPlane` ends a viewer's reader session (MediaMTX's control API).
* :class:`EventPublisher` publishes the lifecycle events (stream ``CAMERA``).
* :class:`TicketSigner` signs and verifies tickets.
* :class:`RelayRepository` keeps the sessions (in memory: one replica, lost on restart).
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from mowdeck_contracts.v1 import EventEnvelope
from mowdeck_contracts.v1.camera import CameraInfo, CameraProvider
from mowdeck_contracts.v1.relay import RelayErrorCode

from camera_relay.domain.effects import StartVendorSession, StopVendorSession, UpdateCameras
from camera_relay.domain.models import RelayKey
from camera_relay.domain.policy import MowerStatus
from camera_relay.domain.session import RelaySession
from camera_relay.domain.tickets import TicketClaims


class Clock(Protocol):
    """Returns the current time (timezone-aware)."""

    def __call__(self) -> datetime:
        """The current time."""
        ...


class SourceError(Exception):
    """The source could not be started; ``code`` is the reply's error code."""

    code: RelayErrorCode = RelayErrorCode.UNAVAILABLE


class SourceUnavailableError(SourceError):
    """The mower, the vendor or the source driver cannot deliver a session now."""

    code = RelayErrorCode.UNAVAILABLE


class UnsupportedProviderError(SourceError):
    """No source driver for the mower's ``CameraProvider`` (a client in ``auto`` falls back)."""

    code = RelayErrorCode.UNSUPPORTED_PROVIDER


class ForbiddenSourceError(SourceError):
    """The gateway refused the session: the account does not own the mower."""

    code = RelayErrorCode.FORBIDDEN


@dataclass(frozen=True, slots=True)
class SourceOffer:
    """What a vendor session offers: its provider (which has a source driver) and cameras."""

    provider: CameraProvider
    cameras: tuple[CameraInfo, ...]
    quota_remaining_percent: float | None = None
    """Remaining share of the vendor's video budget, when the vendor session reports one (the
    Vision limit below 10 %)."""


class VendorSessions(Protocol):
    """The source of a session. ``offer`` asks for the vendor session before the first lease
    starts it; ``start`` only requests the media: it arrives later and is reported through
    ``RelayService.source_started`` (or ``source_failed``)."""

    async def offer(self, key: RelayKey) -> SourceOffer:
        """The vendor session the next ``start`` of ``key`` uses: its provider and cameras.

        Raise :class:`UnsupportedProviderError` when no driver serves its provider (the vendor
        session is left alone, a direct viewer may use it) or another :class:`SourceError`.
        """
        ...

    async def discard(self, key: RelayKey) -> None:
        """Release the offered vendor session of ``key`` that no start will use (the attach was
        refused after the offer)."""
        ...

    async def start(self, command: StartVendorSession) -> None:
        """Request the source; raise :class:`SourceError` when it cannot start."""
        ...

    async def update_cameras(self, command: UpdateCameras) -> None:
        """Change the active cameras of a running source."""
        ...

    async def stop(self, command: StopVendorSession) -> None:
        """Leave the source and, with ``release``, release the vendor session."""
        ...


class MediaPlane(Protocol):
    """The media server's view of the readers."""

    async def kick(self, whep_session: str) -> None:
        """End one reader session."""
        ...


class EventPublisher(Protocol):
    """Publishes relay events."""

    async def publish(self, subject: str, envelope: EventEnvelope[Any]) -> None:
        """Publish ``envelope`` on ``subject`` (``camera.mower.<id>.relay.<kind>``)."""
        ...


class TicketSigner(Protocol):
    """Signs ticket scopes and verifies tickets."""

    def sign(self, claims: TicketClaims) -> str:
        """The signed ticket of ``claims``."""
        ...

    def verify(self, ticket: str, now: datetime) -> TicketClaims:
        """The scope of a valid ticket; raises ``InvalidTicketError`` otherwise."""
        ...


class RelayRepository(Protocol):
    """Sessions per key and the last known mower states."""

    def get(self, key: RelayKey) -> RelaySession | None:
        """The session of ``key``, if any."""
        ...

    def get_or_create(self, key: RelayKey) -> RelaySession:
        """The session of ``key``; a new one starts with the mower's last known state."""
        ...

    def key_of_lease(self, lease_id: UUID) -> RelayKey | None:
        """The key whose session holds ``lease_id``."""
        ...

    def session_keys(self) -> list[RelayKey]:
        """Every key with a session."""
        ...

    def keys_of_mower(self, mower_id: UUID) -> Iterable[RelayKey]:
        """The keys of one mower (one per account that uses it)."""
        ...

    def set_status(self, mower_id: UUID, status: MowerStatus) -> None:
        """Remember the mower's state for new sessions."""
        ...

    def status_of(self, mower_id: UUID) -> MowerStatus | None:
        """The mower's last known state."""
        ...

    def remove(self, key: RelayKey) -> None:
        """Forget a dormant session."""
        ...
