"""The theft tracking mode (ADR 0058): sessions, the background tick, share links, the report.

A session starts on a theft alarm (``notifications.raised`` of kind ``mower_lifted``,
``mower_left_dock_unexpectedly`` or ``geofence_exit``) or by hand, and ends by hand ("false
alarm", "stop") or after the maximum duration. While it runs, :meth:`Tracking.tick` (one replica
at a time, advisory lock) renews the Device-Gateway's lease, reads the newest state of the mower,
follows its position, and publishes the milestones: started, moved (rarely), stale (once per
outage), ended. Positions themselves are the normal position track of the mower (the gateway
reports them at the session's interval while the lease lasts); the session only remembers its
time range, so a track is read from that range.

Share links hold only the SHA-256 of a 256-bit token. The public view answers a token with
exactly the allowed fields and nothing else.
"""

import asyncio
import base64
import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from mowdeck_contracts.v1 import (
    TRACKING_ENDED,
    TRACKING_MOVED,
    TRACKING_STALE,
    TRACKING_STARTED,
    PhotoRequest,
    TrackingEvent,
    TrackingLeaseCode,
    TrackingLeaseRequest,
    VisionDemand,
)
from mowdeck_service_kit import get_logger

from telemetry.application.ports import Clock, Store
from telemetry.application.report import PhotoData, ReportData, build_report
from telemetry.application.tracking_ports import TrackingEvents, TrackingGateway, TrackingStore
from telemetry.domain.errors import (
    LinkLimitError,
    NoTrackingError,
    RateLimitedError,
    TrackingStateError,
    UnknownTrackedMowerError,
)
from telemetry.domain.tracking import (
    TrackingLink,
    TrackingPhoto,
    TrackingSession,
    distance_m,
    is_plausible_token,
    new_token,
    thin,
    token_hash,
)

_log = get_logger("telemetry.tracking")


@dataclass(frozen=True, slots=True)
class TrackingConfig:
    """Everything the tracking mode reads from the settings."""

    interval_seconds: float = 10.0
    """Position report interval asked from the gateway."""
    max_duration: timedelta = timedelta(hours=24)
    """A session ends by itself after this long."""
    tick_seconds: float = 15.0
    """How often the tick runs (lease renewal, stale and move checks)."""
    lease_seconds: float = 60.0
    """Length of the gateway lease; the tick renews it, so a dead Telemetry costs this much."""
    stale_after: timedelta = timedelta(seconds=120)
    """No report for this long marks the session stale (and notifies once)."""
    move_meters: float = 50.0
    """A "moved" notice needs at least this distance from the last notice's position."""
    move_notice_interval: timedelta = timedelta(minutes=5)
    """... and at least this long since the last notice."""
    link_ttl: timedelta = timedelta(hours=48)
    """Lifetime of a share link (also the longest a caller may ask for)."""
    max_links: int = 10
    """Active share links per session."""
    retention: timedelta = timedelta(days=30)
    """Ended sessions (with links and photos) are deleted after this long."""
    auto_start: bool = True
    """Start a session on a theft alarm."""
    photos: bool = True
    """Take camera photos for the report."""
    photos_max: int = 5
    photo_timeout_seconds: float = 30.0
    track_limit: int = 20_000
    """Most positions one view or report reads."""
    public_points: int = 1500
    """Most track points the public page gets."""
    public_rate_per_minute: int = 60
    public_failures_per_minute: int = 20


class SlidingWindow:
    """A small in-process rate limiter: at most ``limit`` hits per key within ``window``."""

    def __init__(self, limit: int, window: timedelta, clock: Clock, max_keys: int = 10_000) -> None:
        self._limit = limit
        self._window = window
        self._clock = clock
        self._max_keys = max_keys
        self._hits: dict[str, list[datetime]] = {}

    def retry_after(self, key: str) -> int:
        """Seconds until ``key`` may hit again; 0 when it may now (does not record)."""
        now = self._clock()
        recent = [t for t in self._hits.get(key, []) if now - t < self._window]
        if len(recent) < self._limit:
            return 0
        return max(1, int((recent[0] + self._window - now).total_seconds()) + 1)

    def hit(self, key: str) -> None:
        """Record a hit of ``key``."""
        now = self._clock()
        if len(self._hits) >= self._max_keys:
            self._hits = {k: v for k, v in self._hits.items() if v and now - v[-1] < self._window}
            if len(self._hits) >= self._max_keys:
                self._hits.clear()  # an attack on the limiter itself: start over
        recent = [t for t in self._hits.get(key, []) if now - t < self._window]
        recent.append(now)
        self._hits[key] = recent


@dataclass(frozen=True, slots=True)
class TrackPoint:
    """One position of a track."""

    lat: float
    lon: float
    at: datetime


@dataclass(frozen=True, slots=True)
class TrackingView:
    """A session with its track and links (the owner's view)."""

    session: TrackingSession
    track: list[TrackPoint]
    links: list[TrackingLink]
    photos: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class PublicTrack:
    """What a share link may show: nothing but this."""

    mower_name: str
    mower_model: str | None
    active: bool
    started_at: datetime
    ended_at: datetime | None
    last: TrackPoint | None
    stale_since: datetime | None
    track: list[TrackPoint]
    expires_at: datetime
    server_time: datetime


class Tracking:
    """The theft tracking use cases."""

    def __init__(
        self,
        store: TrackingStore,
        data: Store,
        config: TrackingConfig,
        clock: Clock,
        events: TrackingEvents,
        gateway: TrackingGateway,
    ) -> None:
        self._store = store
        self._data = data
        self._config = config
        self._clock = clock
        self._events = events
        self._gateway = gateway
        self._tasks: set[asyncio.Task[None]] = set()
        self._public_ok = SlidingWindow(config.public_rate_per_minute, timedelta(minutes=1), clock)
        self._public_bad = SlidingWindow(
            config.public_failures_per_minute, timedelta(minutes=1), clock
        )

    def now(self) -> datetime:
        """The current time of the tracking clock."""
        return self._clock()

    @property
    def config(self) -> TrackingConfig:
        """The configuration in force."""
        return self._config

    # --- sessions -----------------------------------------------------------------------------

    async def start(
        self, account_id: UUID, mower_id: UUID, trigger: str, *, user_id: UUID | None = None
    ) -> tuple[TrackingSession, bool]:
        """Start a session (or return the running one); True when it was created now."""
        entry = await self._data.directory_of(mower_id)
        if entry is None or entry.account_id != account_id:
            raise UnknownTrackedMowerError("unknown mower")
        running = await self._store.active_of(account_id, mower_id)
        if running is not None:
            return running, False
        now = self._clock()
        latest = await self._data.latest_of(account_id, mower_id)
        lat, lon, fix_at = (
            _position_of(latest.state, latest.observed_at) if latest else (None, None, None)
        )
        capabilities = await self._data.snapshot_of(account_id, mower_id, "capabilities")
        model = capabilities.body.get("model") if capabilities else None
        session = TrackingSession(
            id=uuid4(),
            account_id=account_id,
            mower_id=mower_id,
            trigger=trigger,
            started_at=now,
            ends_at=now + self._config.max_duration,
            interval_seconds=self._config.interval_seconds,
            mower_name=entry.name or "MowDeck",
            mower_model=model if isinstance(model, str) else None,
            mower_serial=entry.serial,
            started_by=user_id,
            last_lat=lat,
            last_lon=lon,
            last_fix_at=fix_at,
            notice_lat=lat,
            notice_lon=lon,
            notice_at=now if lat is not None else None,
        )
        if not await self._store.add_session(session):
            running = await self._store.active_of(account_id, mower_id)
            if running is None:  # ended in between: let the caller retry
                raise TrackingStateError("the tracking session changed, try again")
            return running, False
        _log.info(
            "tracking started",
            mower_id=str(mower_id),
            session_id=str(session.id),
            trigger=trigger,
        )
        await self._events.publish(TRACKING_STARTED, _event(session, now))
        await self._renew_lease(session)
        if self._config.photos:
            self._spawn(self._take_photos(session))
        return session, True

    async def end(
        self, account_id: UUID, mower_id: UUID, reason: str, *, session_id: UUID | None = None
    ) -> TrackingSession:
        """End the active session of a mower ("false_alarm" or "stopped")."""
        session = await self._store.active_of(account_id, mower_id)
        if session is None or (session_id is not None and session.id != session_id):
            raise NoTrackingError("no active tracking session")
        return await self._finish(session, reason)

    async def _finish(self, session: TrackingSession, reason: str) -> TrackingSession:
        now = self._clock()
        points = len(await self._track(session, now, limit=self._config.track_limit))
        ended = session.evolve(ended_at=now, end_reason=reason, points=points)
        await self._store.put_session(ended)
        with contextlib.suppress(Exception):
            await self._gateway.lease(
                TrackingLeaseRequest(
                    mower_id=session.mower_id,
                    account_id=session.account_id,
                    interval_seconds=session.interval_seconds,
                    lease_seconds=0,
                )
            )
        _log.info(
            "tracking ended",
            mower_id=str(session.mower_id),
            session_id=str(session.id),
            reason=reason,
        )
        await self._events.publish(TRACKING_ENDED, _event(ended, now))
        return ended

    async def view(
        self,
        account_id: UUID,
        mower_id: UUID,
        *,
        session_id: UUID | None = None,
        after: datetime | None = None,
    ) -> TrackingView | None:
        """The active session of a mower, else the newest one; None when it never had one.
        With ``after`` the track holds only the newer positions."""
        session = await self._pick(account_id, mower_id, session_id)
        if session is None:
            return None
        now = self._clock()
        track = await self._track(session, now, after=after, limit=self._config.track_limit)
        return TrackingView(
            session=session,
            track=track,
            links=await self._store.links_of(session.id),
            photos=len(await self._store.photos_of(session.id)),
            truncated=len(track) >= self._config.track_limit,
        )

    async def sessions(
        self, account_id: UUID, mower_id: UUID, limit: int = 20
    ) -> list[TrackingSession]:
        """Sessions of a mower, newest first."""
        return await self._store.sessions_of(account_id, mower_id, limit)

    async def delete(self, account_id: UUID, mower_id: UUID, session_id: UUID) -> None:
        """Delete an ended session with its links and photos (the position track of the mower
        stays under its own retention)."""
        session = await self._store.session_of(account_id, session_id)
        if session is None or session.mower_id != mower_id:
            raise NoTrackingError("unknown tracking session")
        if session.active:
            raise TrackingStateError("end the tracking before deleting it")
        await self._store.delete_session(account_id, session_id)

    async def _pick(
        self, account_id: UUID, mower_id: UUID, session_id: UUID | None
    ) -> TrackingSession | None:
        if session_id is not None:
            found = await self._store.session_of(account_id, session_id)
            return found if found is not None and found.mower_id == mower_id else None
        return await self._store.active_of(account_id, mower_id) or await self._store.latest_of(
            account_id, mower_id
        )

    async def _track(
        self,
        session: TrackingSession,
        now: datetime,
        *,
        after: datetime | None = None,
        limit: int,
    ) -> list[TrackPoint]:
        start = (
            max(session.started_at, after + timedelta(microseconds=1))
            if after
            else session.started_at
        )
        end = (session.ended_at or now) + timedelta(seconds=1)
        if start >= end:
            return []
        rows = await self._data.positions(session.account_id, session.mower_id, start, end, limit)
        return [TrackPoint(r.lat, r.lon, r.observed_at) for r in rows]

    # --- background tick ----------------------------------------------------------------------

    async def tick(self) -> int:
        """One pass over the active sessions (only on the replica holding the lock); returns
        how many it looked at."""
        async with self._store.leader() as leader:
            if not leader:
                return 0
            sessions = await self._store.active_sessions()
            for session in sessions:
                try:
                    await self._tick_one(session)
                except Exception:
                    _log.exception("tracking tick failed", session_id=str(session.id))
            return len(sessions)

    async def _tick_one(self, session: TrackingSession) -> None:
        now = self._clock()
        if now >= session.ends_at:
            await self._finish(session, "max_duration")
            return
        latest = await self._data.latest_of(session.account_id, session.mower_id)
        changes: dict[str, Any] = {}
        events: list[tuple[str, TrackingEvent]] = []
        observed = latest.observed_at if latest else None
        if latest is not None:
            lat, lon, fix_at = _position_of(latest.state, latest.observed_at)
            if lat is not None and lon is not None and fix_at != session.last_fix_at:
                changes.update(last_lat=lat, last_lon=lon, last_fix_at=fix_at)
        connected = bool(latest.state.get("connected", True)) if latest else False
        silent = observed is None or now - observed > self._config.stale_after or not connected
        if silent:
            if session.stale_since is None:
                changes["stale_since"] = (
                    observed + self._config.stale_after if observed else session.started_at
                )
            if not session.stale_notified:
                since = observed or session.started_at
                minutes = max(1, int((now - since).total_seconds() // 60))
                changes["stale_notified"] = True
                events.append(
                    (
                        TRACKING_STALE,
                        _event(session.evolve(**changes), now, stale_minutes=minutes),
                    )
                )
        elif session.stale_since is not None or session.stale_notified:
            changes.update(stale_since=None, stale_notified=False)
        current = session.evolve(**changes)
        if current.last_lat is not None and current.last_lon is not None:
            moved = self._moved(current, now)
            if current.notice_lat is None:  # the first position: the reference for later notices
                changes.update(
                    notice_lat=current.last_lat, notice_lon=current.last_lon, notice_at=now
                )
            elif moved is not None:
                changes.update(
                    notice_lat=current.last_lat, notice_lon=current.last_lon, notice_at=now
                )
                events.append((TRACKING_MOVED, _event(current, now, distance_m=moved)))
        if changes:
            await self._store.put_session(session.evolve(**changes))
        for event_type, event in events:
            await self._events.publish(event_type, event)
        await self._renew_lease(session)

    def _moved(self, session: TrackingSession, now: datetime) -> float | None:
        """Metres moved since the last notice when a new notice is due, else None."""
        assert session.last_lat is not None and session.last_lon is not None  # noqa: S101
        if session.notice_lat is None or session.notice_lon is None or session.notice_at is None:
            return None  # no reference yet: the next change of the notice reference sets it
        if now - session.notice_at < self._config.move_notice_interval:
            return None
        metres = distance_m(
            session.notice_lat, session.notice_lon, session.last_lat, session.last_lon
        )
        return metres if metres >= self._config.move_meters else None

    async def _renew_lease(self, session: TrackingSession) -> None:
        request = TrackingLeaseRequest(
            mower_id=session.mower_id,
            account_id=session.account_id,
            interval_seconds=session.interval_seconds,
            lease_seconds=self._config.lease_seconds,
        )
        try:
            reply = await self._gateway.lease(request)
        except Exception as exc:
            _log.warning("tracking lease failed", mower_id=str(session.mower_id), error=repr(exc))
            return
        if reply is None:
            _log.warning("no gateway answered the tracking lease", mower_id=str(session.mower_id))
        elif reply.code is not TrackingLeaseCode.OK:
            _log.info(
                "tracking lease not taken", mower_id=str(session.mower_id), code=reply.code.value
            )

    async def retain(self) -> int:
        """Delete ended sessions past the retention (with their links and photos)."""
        return await self._store.delete_ended_before(self._clock() - self._config.retention)

    # --- photos -------------------------------------------------------------------------------

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def close(self) -> None:
        """Cancel the background photo tasks."""
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _take_photos(self, session: TrackingSession) -> None:
        """One photo at the start, a second try a minute later when the first failed. A lifted
        mower is an error state, so the request uses the error analysis demand (it may wake a
        sleeping camera and ignores the Vision budget, ADR 0045)."""
        for delay in (3.0, 60.0):
            await asyncio.sleep(delay)
            if await self.take_photo(session.account_id, session.mower_id, session.id):
                return

    async def take_photo(self, account_id: UUID, mower_id: UUID, session_id: UUID) -> bool:
        """Capture one camera image for a session; False when none could be taken."""
        session = await self._store.session_of(account_id, session_id)
        if session is None or session.mower_id != mower_id:
            raise NoTrackingError("unknown tracking session")
        if len(await self._store.photos_of(session_id)) >= self._config.photos_max:
            raise TrackingStateError("the session already has the most photos")
        try:
            reply = await self._gateway.photo(
                PhotoRequest(
                    mower_id=mower_id, account_id=account_id, demand=VisionDemand.ERROR_ANALYSIS
                )
            )
        except Exception as exc:
            _log.warning("tracking photo failed", mower_id=str(mower_id), error=repr(exc))
            return False
        if (
            reply is None
            or reply.error is not None
            or not reply.image_base64
            or not reply.media_type
        ):
            return False
        try:
            data = base64.b64decode(reply.image_base64, validate=True)
        except ValueError:
            return False
        await self._store.add_photo(
            TrackingPhoto(
                id=uuid4(),
                session_id=session_id,
                account_id=account_id,
                taken_at=reply.taken_at or self._clock(),
                media_type=reply.media_type,
                data=data,
            )
        )
        return True

    # --- share links --------------------------------------------------------------------------

    async def create_link(
        self,
        account_id: UUID,
        mower_id: UUID,
        *,
        session_id: UUID | None = None,
        ttl: timedelta | None = None,
        user_id: UUID | None = None,
    ) -> tuple[TrackingLink, str]:
        """Issue a share link for the session; returns the link and its token (shown once)."""
        session = await self._pick(account_id, mower_id, session_id)
        if session is None:
            raise NoTrackingError("no tracking session")
        now = self._clock()
        active = [link for link in await self._store.links_of(session.id) if link.usable(now)]
        if len(active) >= self._config.max_links:
            raise LinkLimitError(f"at most {self._config.max_links} active links per session")
        lifetime = min(ttl or self._config.link_ttl, self._config.link_ttl)
        token = new_token()
        link = TrackingLink(
            id=uuid4(),
            session_id=session.id,
            account_id=account_id,
            mower_id=mower_id,
            token_hash=token_hash(token),
            created_at=now,
            expires_at=now + lifetime,
            created_by=user_id,
        )
        await self._store.add_link(link)
        _log.info("tracking link issued", session_id=str(session.id), link_id=str(link.id))
        return link, token

    async def links(
        self, account_id: UUID, mower_id: UUID, session_id: UUID | None = None
    ) -> list[TrackingLink]:
        """Links of the session (active or newest one)."""
        session = await self._pick(account_id, mower_id, session_id)
        return await self._store.links_of(session.id) if session else []

    async def revoke_link(self, account_id: UUID, link_id: UUID) -> None:
        """Revoke a link at once."""
        if not await self._store.revoke_link(account_id, link_id, self._clock()):
            raise NoTrackingError("unknown or already revoked link")
        _log.info("tracking link revoked", link_id=str(link_id))

    async def public_view(self, token: str, client: str) -> PublicTrack | None:
        """What a share link shows, or None for any unusable token (unknown, expired, revoked
        look the same). Raises :class:`RateLimitedError` for a client that asks too often."""
        wait = max(self._public_bad.retry_after(client), self._public_ok.retry_after(client))
        if wait:
            raise RateLimitedError("too many requests", wait)
        self._public_ok.hit(client)
        now = self._clock()
        link = (
            await self._store.link_by_hash(token_hash(token)) if is_plausible_token(token) else None
        )
        if link is None or not link.usable(now):
            self._public_bad.hit(client)
            return None
        session = await self._store.session_of(link.account_id, link.session_id)
        if session is None:
            self._public_bad.hit(client)
            return None
        await self._store.touch_link(link.id, now)
        last: TrackPoint | None = None
        if session.active:
            latest = await self._data.latest_of(session.account_id, session.mower_id)
            if latest is not None:
                lat, lon, at = _position_of(latest.state, latest.observed_at)
                if lat is not None and lon is not None and at is not None:
                    last = TrackPoint(lat, lon, at)
        track = await self._track(session, now, limit=self._config.track_limit)
        if last is None and session.last_lat is not None and session.last_lon is not None:
            last = TrackPoint(
                session.last_lat, session.last_lon, session.last_fix_at or session.started_at
            )
        if track:
            last = last or track[-1]
        points = [(p.lat, p.lon, p.at) for p in track]
        return PublicTrack(
            mower_name=session.mower_name,
            mower_model=session.mower_model,
            active=session.active,
            started_at=session.started_at,
            ended_at=session.ended_at,
            last=last,
            stale_since=session.stale_since if session.active else None,
            track=[TrackPoint(*p) for p in thin(points, self._config.public_points)],
            expires_at=link.expires_at,
            server_time=now,
        )

    # --- report -------------------------------------------------------------------------------

    async def report(
        self, account_id: UUID, mower_id: UUID, session_id: UUID | None, lang: str
    ) -> tuple[bytes, TrackingSession]:
        """The theft report PDF of a session (the active or newest one without ``session_id``)."""
        session = await self._pick(account_id, mower_id, session_id)
        if session is None:
            raise NoTrackingError("no tracking session")
        now = self._clock()
        track = await self._track(session, now, limit=self._config.track_limit)
        photos = [PhotoData(p.taken_at, p.data) for p in await self._store.photos_of(session.id)]
        pdf = build_report(
            ReportData(
                mower_name=session.mower_name,
                mower_model=session.mower_model,
                mower_serial=session.mower_serial,
                trigger=session.trigger,
                started_at=session.started_at,
                ended_at=session.ended_at,
                generated_at=now.astimezone(UTC),
                points=[(p.lat, p.lon, p.at) for p in track],
                photos=photos,
            ),
            lang,
        )
        return pdf, session


def _position_of(
    state: dict[str, Any], observed_at: datetime
) -> tuple[float | None, float | None, datetime | None]:
    """Latitude, longitude and report time of a stored state's position (None without one)."""
    position = state.get("position")
    if not isinstance(position, dict):
        return None, None, None
    lat, lon = position.get("lat"), position.get("lon")
    if not isinstance(lat, int | float) or not isinstance(lon, int | float):
        return None, None, None
    return float(lat), float(lon), observed_at


def _event(
    session: TrackingSession,
    at: datetime,
    *,
    distance_m: float | None = None,
    stale_minutes: int | None = None,
) -> TrackingEvent:
    return TrackingEvent(
        session_id=session.id,
        mower_id=session.mower_id,
        account_id=session.account_id,
        at=at,
        trigger=session.trigger,  # type: ignore[arg-type]
        end_reason=session.end_reason,  # type: ignore[arg-type]
        lat=session.last_lat,
        lon=session.last_lon,
        distance_m=distance_m,
        stale_minutes=stale_minutes,
        track_points=session.points if session.ended_at is not None else None,
    )
