"""Keys, states and leases of the relay."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from mowdeck_contracts.v1.principal import PrincipalRef
from mowdeck_contracts.v1.relay import RelayLeaseCloseReason, RelayPurpose


@dataclass(frozen=True, slots=True, order=True)
class RelayKey:
    """One source session at most per key. The same mower under two accounts is two keys:
    nothing (session, paths, tickets, leases, limits) is shared between them."""

    account_id: UUID
    mower_id: UUID


class SessionState(StrEnum):
    """Lifecycle of the session of one key (ADR 0045)."""

    IDLE = "idle"
    """No source session."""
    STARTING = "starting"
    """The source was asked to start; no media yet."""
    LIVE = "live"
    """Media flows and at least one lease holds the session."""
    DRAINING = "draining"
    """The last lease closed; the source stays up for the grace period."""
    STOPPING = "stopping"
    """The source is being left and the stream released."""
    FAILED = "failed"
    """The source failed; new starts wait for the backoff."""


ACTIVE_STATES = frozenset({SessionState.STARTING, SessionState.LIVE, SessionState.DRAINING})
"""States in which the source runs (or is being started)."""


class VisionMode(StrEnum):
    """How long a Vision lease may hold the session."""

    SNAPSHOT = "snapshot"
    """A single frame: at most ``VISION_SNAPSHOT_HOLD_SECONDS``, never extended."""
    STREAM = "stream"
    """A frame stream: at most ``VISION_STREAM_LEASE_SECONDS`` after the last pull (renew)."""


@dataclass(slots=True)
class Lease:
    """One holder of a session: a viewer (a person) or Vision.

    A viewer lease is *present* only while its media is confirmed (a WHEP reader bound to it
    receives bytes; one reader per camera, so a viewer with both front cameras side by side has
    two) and its heartbeat is fresh. Until it is confirmed it lives at most
    ``bind_deadline``; without a heartbeat it ends at ``expires_at``. A Vision lease ends at
    ``expires_at`` (its cap) or when its request ends (detach).
    """

    lease_id: UUID
    key: RelayKey
    camera_ids: tuple[str, ...]
    purpose: RelayPurpose
    principal: PrincipalRef
    opened_at: datetime
    expires_at: datetime
    """Heartbeat deadline of a viewer, cap of a Vision lease."""
    bind_deadline: datetime | None = None
    """Viewer without confirmed media: closed at this time unless media is confirmed."""
    readers: dict[str, str] = field(default_factory=dict)
    """The media plane's reader session per camera (tile) bound to this viewer (empty while
    unconfirmed). A new reader of the same camera replaces the old one (a reconnect)."""
    was_bound: bool = False
    """The viewer had confirmed media once (its later loss closes it as ``media_lost``)."""
    vision_mode: VisionMode | None = None

    @property
    def is_viewer(self) -> bool:
        """A person watches."""
        return self.purpose is RelayPurpose.VIEWER

    @property
    def is_vision(self) -> bool:
        """Vision pulls frames."""
        return self.purpose is RelayPurpose.VISION

    @property
    def whep_session(self) -> str | None:
        """The first bound reader session (None while unconfirmed)."""
        return next(iter(self.readers.values()), None)

    @property
    def present(self) -> bool:
        """A viewer with confirmed media (its heartbeat is fresh while the lease exists)."""
        return self.is_viewer and bool(self.readers)

    def deadline(self) -> tuple[datetime, RelayLeaseCloseReason]:
        """When this lease ends unless something happens, and why."""
        if self.bind_deadline is not None and self.bind_deadline <= self.expires_at:
            reason = (
                RelayLeaseCloseReason.MEDIA_LOST
                if self.was_bound
                else RelayLeaseCloseReason.EXPIRED
            )
            return self.bind_deadline, reason
        return self.expires_at, RelayLeaseCloseReason.EXPIRED
