"""Ports: what the use cases need from the outside world, as protocols."""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from mowdeck_contracts.v1 import (
    CommandEnvelope,
    DriveStartReply,
    DriveStartRequest,
    DriveStopRequest,
    Principal,
)

from realtime.domain.models import Authenticated


class Clock(Protocol):
    """Returns the current time (timezone-aware)."""

    def __call__(self) -> datetime:
        """The current time."""
        ...


class TokenVerifier(Protocol):
    """Turns a raw access token into a principal (``mowdeck-auth`` in production)."""

    async def verify(self, token: str) -> Authenticated:
        """The principal and expiry; raises ``InvalidTokenError``."""
        ...


class AccessChecker(Protocol):
    """Answers ``can_view`` on mowers (OpenFGA through ``mowdeck-authz``)."""

    async def can_view(self, principal: Principal, mower_ids: Sequence[UUID]) -> list[bool]:
        """One answer per id, in order; failures answer False (fail closed)."""
        ...

    async def can_drive(self, principal: Principal, mower_id: UUID) -> bool:
        """``can_drive`` on one mower; failures answer False (fail closed)."""
        ...

    async def available(self) -> bool:
        """False while the authorization service cannot answer (re-checks are skipped then,
        so an outage does not revoke every subscription)."""
        ...

    def subject_key(self, principal: Principal) -> str:
        """The subject as named in ``identity.grants.changed`` (``user:<id>``)."""
        ...


class DriveBackend(Protocol):
    """Fleet's drive session requests and the command bus (``adapters.bus.drive``)."""

    async def start(self, request: DriveStartRequest) -> DriveStartReply:
        """``fleet.drive.start``; raises ``DriveUnavailableError`` when Fleet cannot answer."""
        ...

    async def stop(self, request: DriveStopRequest) -> None:
        """``fleet.drive.stop``, best effort (failures are logged; Fleet expires the record)."""
        ...

    async def publish(self, envelope: CommandEnvelope) -> None:
        """Publish one frame on ``command.mower.<id>``; raises when the bus cannot take it."""
        ...


class Socket(Protocol):
    """One accepted WebSocket, seen from the use cases."""

    async def send_text(self, text: str) -> None:
        """Send one text frame (raises when the peer is gone)."""
        ...

    async def close(self, code: int, reason: str) -> None:
        """Send a close frame."""
        ...


class Metrics(Protocol):
    """Counters and gauges of the hub (Prometheus in production)."""

    def connections(self, count: int) -> None:
        """Open sockets of this instance."""
        ...

    def subscriptions(self, count: int) -> None:
        """Subscribed (socket, mower) pairs of this instance."""
        ...

    def sent(self, message_type: str) -> None:
        """A message was written to a socket."""
        ...

    def dropped(self, reason: str) -> None:
        """A message was not delivered: ``coalesced`` (a newer state replaced it),
        ``slow_consumer``, ``account_mismatch`` or ``invalid``."""
        ...

    def closed(self, code: int | None) -> None:
        """A socket ended with this close code (None: the client went away)."""
        ...

    def drive_sessions(self, count: int) -> None:
        """Open drive sessions of this instance."""
        ...

    def drive_frame(self, outcome: str) -> None:
        """A joystick frame: ``published``, ``rate_limited`` or ``failed``."""
        ...


class NoMetrics:
    """A :class:`Metrics` that records nothing."""

    def connections(self, count: int) -> None:
        """Ignored."""

    def subscriptions(self, count: int) -> None:
        """Ignored."""

    def sent(self, message_type: str) -> None:
        """Ignored."""

    def dropped(self, reason: str) -> None:
        """Ignored."""

    def closed(self, code: int | None) -> None:
        """Ignored."""

    def drive_sessions(self, count: int) -> None:
        """Ignored."""

    def drive_frame(self, outcome: str) -> None:
        """Ignored."""
