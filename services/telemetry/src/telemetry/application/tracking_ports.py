"""Ports of the theft tracking mode (ADR 0058)."""

from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from mowdeck_contracts.v1 import (
    PhotoReply,
    PhotoRequest,
    TrackingEvent,
    TrackingLeaseReply,
    TrackingLeaseRequest,
)

from telemetry.domain.tracking import TrackingLink, TrackingPhoto, TrackingSession


class TrackingStore(Protocol):
    """Persistence of sessions, share links and photos."""

    async def add_session(self, session: TrackingSession) -> bool:
        """Insert a session; False when the mower already has an active one."""
        ...

    async def put_session(self, session: TrackingSession) -> None:
        """Replace a session row."""
        ...

    async def update_active(self, session_id: UUID, changes: Mapping[str, Any]) -> bool:
        """Set ``changes`` on a session only while it is still active; False when it ended in
        the meantime (a background write must never undo an end)."""
        ...

    async def session_of(self, account_id: UUID, session_id: UUID) -> TrackingSession | None:
        """One session of an account."""
        ...

    async def active_of(self, account_id: UUID, mower_id: UUID) -> TrackingSession | None:
        """The active session of a mower of an account."""
        ...

    async def latest_of(self, account_id: UUID, mower_id: UUID) -> TrackingSession | None:
        """The newest session (active or ended) of a mower of an account."""
        ...

    async def sessions_of(
        self, account_id: UUID, mower_id: UUID, limit: int
    ) -> list[TrackingSession]:
        """Sessions of a mower, newest first."""
        ...

    async def active_sessions(self) -> list[TrackingSession]:
        """Every active session (all accounts; for the background tick only)."""
        ...

    async def delete_session(self, account_id: UUID, session_id: UUID) -> bool:
        """Delete an ended session with its links and photos; False when none matched."""
        ...

    async def delete_ended_before(self, cutoff: datetime) -> int:
        """Delete sessions that ended before ``cutoff`` (retention); returns how many."""
        ...

    async def add_link(self, link: TrackingLink) -> None:
        """Insert a link."""
        ...

    async def links_of(self, session_id: UUID) -> list[TrackingLink]:
        """Links of a session, newest first."""
        ...

    async def link_by_hash(self, token_hash: str) -> TrackingLink | None:
        """The link whose token has this hash."""
        ...

    async def revoke_link(self, account_id: UUID, link_id: UUID, now: datetime) -> bool:
        """Revoke a link of an account; False when none matched or it was revoked already."""
        ...

    async def touch_link(self, link_id: UUID, now: datetime) -> None:
        """Count a use of the link."""
        ...

    async def add_photo(self, photo: TrackingPhoto) -> None:
        """Insert a photo."""
        ...

    async def photos_of(self, session_id: UUID) -> list[TrackingPhoto]:
        """Photos of a session, oldest first."""
        ...

    def leader(self) -> AbstractAsyncContextManager[bool]:
        """Try to become the replica that runs the background tick; yields False when another
        replica is running it."""
        ...


class TrackingEvents(Protocol):
    """Publishes the tracking events (``event.tracking.*``)."""

    async def publish(self, event_type: str, event: TrackingEvent) -> None:
        """Publish one event (no-op without a bus)."""
        ...


class TrackingGateway(Protocol):
    """Talks to the Device-Gateway that holds the mower."""

    async def lease(self, request: TrackingLeaseRequest) -> TrackingLeaseReply | None:
        """Take, renew (``lease_seconds`` above 0) or release a tracking lease; None when no
        gateway answered."""
        ...

    async def photo(self, request: PhotoRequest) -> PhotoReply | None:
        """Ask for a still image; None when no one answered."""
        ...
