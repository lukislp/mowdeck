"""Theft tracking sessions, share links and photos (ADR 0058): plain data and pure rules."""

import hashlib
import math
import secrets
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

THEFT_ALARM_KINDS = frozenset({"mower_lifted", "mower_left_dock_unexpectedly", "geofence_exit"})
"""Notification kinds (ADR 0027) that start a tracking session on their own."""

TOKEN_BYTES = 32
"""Entropy of a share token: 256 bits."""

_EARTH_RADIUS_M = 6_371_000.0


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great circle distance in metres (haversine)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


@dataclass(frozen=True, slots=True)
class TrackingSession:
    """One theft tracking run of one mower (table ``tracking_sessions``).

    At most one session per mower is ``active``. ``ends_at`` is the automatic end;
    ``last_*`` is the newest position seen, ``stale_since`` is set while the mower has not
    reported for longer than the stale threshold. ``notice_*`` is the reference of the last
    "moved" notice.
    """

    id: UUID
    account_id: UUID
    mower_id: UUID
    trigger: str
    started_at: datetime
    ends_at: datetime
    interval_seconds: float
    mower_name: str
    mower_model: str | None = None
    mower_serial: str | None = None
    started_by: UUID | None = None
    ended_at: datetime | None = None
    end_reason: str | None = None
    last_lat: float | None = None
    last_lon: float | None = None
    last_fix_at: datetime | None = None
    stale_since: datetime | None = None
    stale_notified: bool = False
    notice_lat: float | None = None
    notice_lon: float | None = None
    notice_at: datetime | None = None
    points: int = 0

    @property
    def active(self) -> bool:
        """Whether the session still runs."""
        return self.ended_at is None

    def evolve(self, **changes: object) -> "TrackingSession":
        """A copy with ``changes``."""
        return replace(self, **changes)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class TrackingLink:
    """A secret share link of one session (table ``tracking_links``). Only the SHA-256 of the
    token is stored; the token itself is shown once, when the link is created."""

    id: UUID
    session_id: UUID
    account_id: UUID
    mower_id: UUID
    token_hash: str
    created_at: datetime
    expires_at: datetime
    created_by: UUID | None = None
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None
    uses: int = 0

    def usable(self, now: datetime) -> bool:
        """Neither revoked nor expired."""
        return self.revoked_at is None and now < self.expires_at


@dataclass(frozen=True, slots=True)
class TrackingPhoto:
    """A camera image taken for a session (table ``tracking_photos``)."""

    id: UUID
    session_id: UUID
    account_id: UUID
    taken_at: datetime
    media_type: Literal["image/jpeg", "image/png"]
    data: bytes


def new_token() -> str:
    """A fresh URL safe share token (256 bits from the system's CSPRNG)."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_hash(token: str) -> str:
    """The stored form of a token: lowercase hex SHA-256. A fast hash is right here: the token
    is 256 bits of randomness, so there is nothing to brute force."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def is_plausible_token(token: str) -> bool:
    """Cheap shape check before any lookup (length and alphabet of :func:`new_token`)."""
    return 40 <= len(token) <= 64 and all(c.isalnum() or c in "-_" for c in token)


def thin(
    points: list[tuple[float, float, datetime]], limit: int
) -> list[tuple[float, float, datetime]]:
    """At most ``limit`` points, evenly spaced, always keeping the first and the last."""
    if limit < 2 or len(points) <= limit:
        return points
    step = (len(points) - 1) / (limit - 1)
    picked = [points[round(i * step)] for i in range(limit)]
    picked[-1] = points[-1]
    return picked


def stale_cutoff(now: datetime, stale_after: timedelta) -> datetime:
    """Reports older than this make a session stale."""
    return now - stale_after
