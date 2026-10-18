"""The relay session of one ``(account_id, mower_id)``: the functional core of the relay.

States: ``IDLE -> STARTING -> LIVE -> DRAINING -> STOPPING -> IDLE``, and ``FAILED`` with a
backoff. Every input takes the current time and returns the effects to carry out; deadlines
(heartbeats, binding, the start timeout, the grace, the backoff, the daily Vision minutes) are
processed in time order by :meth:`RelaySession.tick` and before every other input, each at its
own due time, so event timestamps are exact however late the tick runs.

Rules:

* The active camera set is the union of the open leases' cameras. A lease that names no
  camera gets every camera the source offers (:meth:`RelaySession.offer`, set from the vendor
  session before the first lease starts it); a named camera the source does not offer is left
  out, and a lease with none left is refused.
* Every open lease holds the session: a confirmed viewer while its heartbeat is fresh, an
  unconfirmed viewer only until its binding deadline (``UNBOUND_LEASE_SECONDS``), a Vision lease
  until its cap or its request ends. After the last lease the session drains for the grace
  period; a new lease in that time takes it back to ``LIVE`` without a new source session.
* Vision minutes count only while Vision holds the session without a present viewer; the
  limits of a Vision lease apply only then. Viewers are never refused for a limit (a warning).
* An offline mower (no connection: the gateway's link is down or its heartbeat stale) gets
  no lease, and a running session stops. A sleeping mower may be woken by a viewer, like the
  vendor app does, and by an error analysis (a mower with a problem needs help); routine Vision
  without a present viewer never wakes it and ends when it falls asleep (owner decisions
  2026-10-18). A session with viewers keeps running while the mower reports asleep.
* Error analyses (``VisionDemand.ERROR_ANALYSIS``) are bound only by their per-request caps;
  their time still counts as Vision minutes (for the record), but no limit ends them.
"""

import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID, uuid4

from mowdeck_contracts.v1.camera import CameraInfo, CameraProvider
from mowdeck_contracts.v1.principal import PrincipalRef
from mowdeck_contracts.v1.relay import (
    CAMERA_RELAY_LEASE_CLOSED,
    CAMERA_RELAY_LEASE_OPENED,
    CAMERA_RELAY_STARTED,
    CAMERA_RELAY_STOPPED,
    CameraRelayLeaseClosed,
    CameraRelayLeaseOpened,
    CameraRelayStarted,
    CameraRelayStopped,
    RelayErrorCode,
    RelayLeaseCloseReason,
    RelayPurpose,
    RelayStopReason,
    VisionDemand,
)

from camera_relay.domain.effects import (
    Effect,
    EmitEvent,
    IssueTicket,
    KickViewer,
    StartVendorSession,
    StopVendorSession,
    UpdateCameras,
)
from camera_relay.domain.models import (
    ACTIVE_STATES,
    Lease,
    RelayKey,
    SessionState,
    VisionMode,
)
from camera_relay.domain.policy import MowerStatus, Refusal, RelayPolicy
from camera_relay.domain.tickets import TicketClaims, camera_path


def new_relay_id() -> str:
    """A random 128-bit path token (32 hex characters)."""
    return secrets.token_hex(16)


def _utc_day(at: datetime) -> date:
    return at.astimezone(UTC).date()


def _next_midnight(at: datetime) -> datetime:
    return datetime.combine(_utc_day(at) + timedelta(days=1), time(), tzinfo=UTC)


@dataclass(slots=True)
class AttachDecision:
    """Result of :meth:`RelaySession.attach`: a lease or a refusal, and the effects."""

    lease: Lease | None
    refusal: Refusal | None
    effects: list[Effect]
    budget_warning: bool = False
    """A viewer was admitted although a Vision limit (battery, video budget) is reached."""


@dataclass(slots=True)
class LeaseDecision:
    """Result of an input on one lease: the lease when it is (still) open, and the effects."""

    lease: Lease | None
    effects: list[Effect] = field(default_factory=list)


class RelaySession:
    """The aggregate of one :class:`RelayKey`."""

    def __init__(
        self,
        key: RelayKey,
        *,
        policy: RelayPolicy | None = None,
        status: MowerStatus | None = None,
        new_id: Callable[[], UUID] = uuid4,
        new_path_token: Callable[[], str] = new_relay_id,
    ) -> None:
        self.key = key
        self.policy = policy or RelayPolicy()
        self.status = status
        self._new_id = new_id
        self._new_path_token = new_path_token
        self.state = SessionState.IDLE
        self.session_id: UUID | None = None
        self.relay_id: str | None = None
        self.leases: dict[UUID, Lease] = {}
        self.cameras: tuple[str, ...] = ()
        self.offered: tuple[CameraInfo, ...] = ()
        """The cameras the source offers (from the vendor session), empty while unknown."""
        self.source: CameraProvider | None = None
        self.trigger: RelayPurpose | None = None
        """Purpose of the lease that started the running session."""
        self.started_by: PrincipalRef | None = None
        self.peak_viewers = 0
        """Most viewer leases open at the same time in the running session."""
        self.publishing: tuple[str, ...] = ()
        self.wanted_since: dict[str, datetime] = {}
        """When each camera of the running source was first asked for (for the settle time)."""
        self.started_at: datetime | None = None
        self.start_deadline: datetime | None = None
        self.drain_deadline: datetime | None = None
        self.retry_at: datetime | None = None
        self.failures = 0
        self.session_vision = timedelta(0)
        self._vision_by_day: dict[date, timedelta] = {}
        self._accounted_at: datetime | None = None
        self.silent_cameras: dict[str, datetime] = {}
        """Cameras found silent in an earlier session (camera -> when): reported not available
        from the start for :attr:`RelayPolicy.silent_camera_memory`, until they publish. Kept
        across sessions (the repository hands it on when it forgets a dormant session)."""

    # --- Queries ------------------------------------------------------------------------------

    @property
    def has_present_viewer(self) -> bool:
        """A viewer with confirmed media holds the session."""
        return any(lease.present for lease in self.leases.values())

    @property
    def vision_only(self) -> bool:
        """Vision holds the running session and no viewer is present (Vision minutes count)."""
        return (
            self.state in (SessionState.STARTING, SessionState.LIVE)
            and any(lease.is_vision for lease in self.leases.values())
            and not self.has_present_viewer
        )

    @property
    def routine_vision_only(self) -> bool:
        """Vision-only use with a routine Vision lease (the Vision limits apply)."""
        return self.vision_only and any(lease.is_routine_vision for lease in self.leases.values())

    def vision_used_on(self, day: date) -> timedelta:
        """Vision-only time of this key on a UTC day."""
        return self._vision_by_day.get(day, timedelta(0))

    def is_dormant(self, now: datetime) -> bool:
        """Idle, without leases and without Vision minutes today: safe to forget."""
        return (
            self.state is SessionState.IDLE
            and not self.leases
            and self.vision_used_on(_utc_day(now)) == timedelta(0)
        )

    def claims_for(self, lease: Lease, now: datetime) -> TicketClaims:
        """The scope of a ticket for ``lease``: its account, mower, session, lease and paths."""
        if self.relay_id is None:
            msg = "no session to issue a ticket for"
            raise ValueError(msg)
        return TicketClaims(
            account_id=self.key.account_id,
            mower_id=self.key.mower_id,
            relay_id=self.relay_id,
            lease_id=lease.lease_id,
            paths=tuple(camera_path(self.relay_id, camera) for camera in lease.camera_ids),
            issued_at=now,
            expires_at=now + self.policy.ticket_lifetime,
        )

    def admission(
        self,
        purpose: RelayPurpose,
        now: datetime,
        demand: VisionDemand = VisionDemand.ROUTINE,
    ) -> Refusal | None:
        """Why a lease of ``purpose`` would be refused now, or None (no state change; process
        due deadlines first with :meth:`tick`)."""
        return self._admission(purpose, now, demand)

    def camera_available(self, camera: str, now: datetime) -> bool | None:
        """Whether ``camera`` delivers: True once its stream appeared, False when it has not
        appeared :attr:`RelayPolicy.camera_settle` after the session went live (a listed
        camera that never publishes) or was found silent in a recent session (no flash of a
        camera that never publishes at the start), None while that is not known yet."""
        if camera in self.publishing:
            return True
        found = self.silent_cameras.get(camera)
        if found is not None and now - found < self.policy.silent_camera_memory:
            return False
        if self.state not in (SessionState.LIVE, SessionState.DRAINING) or not self.started_at:
            return None
        since = max(self.started_at, self.wanted_since.get(camera, self.started_at))
        return False if now - since >= self.policy.camera_settle else None

    def camera_infos(self, camera_ids: Sequence[str]) -> list[CameraInfo]:
        """The offered cameras among ``camera_ids``, in that order."""
        by_id = {camera.id: camera for camera in self.offered}
        return [by_id[camera] for camera in camera_ids if camera in by_id]

    # --- Inputs -------------------------------------------------------------------------------

    def offer(self, cameras: Sequence[CameraInfo]) -> None:
        """The cameras of the vendor session the next start will use (``IDLE`` only: a running
        source keeps the cameras it started with)."""
        if self.state is SessionState.IDLE:
            self.offered = tuple(cameras)

    def attach(
        self,
        *,
        camera_ids: tuple[str, ...] | list[str],
        purpose: RelayPurpose,
        principal: PrincipalRef,
        now: datetime,
        vision_mode: VisionMode = VisionMode.STREAM,
        vision_demand: VisionDemand = VisionDemand.ROUTINE,
    ) -> AttachDecision:
        """Open a lease (``relay.camera.attach``). No ``camera_ids``: every offered camera."""
        out: list[Effect] = []
        self._advance(now, out)
        refusal = self._admission(purpose, now, vision_demand)
        if refusal is not None:
            return AttachDecision(None, refusal, out)
        cameras = self._lease_cameras(camera_ids)
        if not cameras:
            detail = (
                "none of the requested cameras is offered by the mower"
                if camera_ids
                else "the mower offers no camera"
            )
            return AttachDecision(None, Refusal(RelayErrorCode.UNAVAILABLE, detail), out)
        warning = purpose is RelayPurpose.VIEWER and (
            self.policy.low_power_or_quota(self.status) is not None
        )
        start: StartVendorSession | None = None
        if self.state is SessionState.IDLE:
            start = self._begin(cameras, now, purpose, principal)
        lease = self._new_lease(cameras, purpose, principal, now, vision_mode)
        if lease.is_vision:
            lease.vision_demand = vision_demand
        self.leases[lease.lease_id] = lease
        viewers = sum(1 for open_lease in self.leases.values() if open_lease.is_viewer)
        self.peak_viewers = max(self.peak_viewers, viewers)
        out.append(self._lease_opened(lease))
        if start is not None:
            out.append(start)
        self._settle(now, out)
        out.append(IssueTicket(self.claims_for(lease, now)))
        return AttachDecision(lease, None, out, budget_warning=warning)

    def renew(self, lease_id: UUID, now: datetime) -> LeaseDecision:
        """The heartbeat of a viewer or a pull of a Vision stream: extend it, issue a ticket.

        A Vision snapshot is never extended.
        """
        out: list[Effect] = []
        self._advance(now, out)
        lease = self.leases.get(lease_id)
        if lease is None:
            return LeaseDecision(None, out)
        if lease.is_viewer:
            lease.expires_at = now + self.policy.lease_ttl
        elif lease.vision_mode is VisionMode.STREAM:
            lease.expires_at = now + self.policy.vision_stream_lease
        out.append(IssueTicket(self.claims_for(lease, now)))
        return LeaseDecision(lease, out)

    def detach(self, lease_id: UUID, now: datetime) -> LeaseDecision:
        """End a lease (page left, tab hidden, Vision request done). Unknown: no-op."""
        out: list[Effect] = []
        self._advance(now, out)
        lease = self.leases.get(lease_id)
        if lease is None:
            return LeaseDecision(None, out)
        self._close(lease, RelayLeaseCloseReason.DETACHED, now, out)
        self._settle(now, out)
        return LeaseDecision(lease, out)

    def media_confirmed(
        self, lease_id: UUID, whep_session: str, now: datetime, camera: str = ""
    ) -> LeaseDecision:
        """The media plane reports bytes flowing to the reader ``whep_session`` of a viewer, on
        the path of ``camera``. One reader per camera: a new reader of the same camera (a
        reconnect) replaces and kicks the old one; readers of other cameras (the tiles of a
        side-by-side view) stay."""
        out: list[Effect] = []
        self._advance(now, out)
        lease = self.leases.get(lease_id)
        if lease is None or not lease.is_viewer:
            return LeaseDecision(None, out)
        previous = lease.readers.get(camera)
        if previous is not None and previous != whep_session:
            out.append(KickViewer(self.key, lease.lease_id, previous))
        for other in [c for c, s in lease.readers.items() if s == whep_session and c != camera]:
            del lease.readers[other]
        lease.readers[camera] = whep_session
        lease.bind_deadline = None
        lease.was_bound = True
        self._settle(now, out)
        return LeaseDecision(lease, out)

    def media_lost(
        self, lease_id: UUID, now: datetime, whep_session: str | None = None
    ) -> LeaseDecision:
        """The viewer's reader ended or stopped receiving media: it may reconnect (with a fresh
        ticket from a renew) until the binding deadline, else it closes as ``media_lost``.

        With ``whep_session`` only that reader counts: the loss of a reader the lease does not
        hold (an attempt that never played, a reader already replaced) changes nothing, and
        while another camera's reader still receives, the lease stays present. Without it every
        reader is gone."""
        out: list[Effect] = []
        self._advance(now, out)
        lease = self.leases.get(lease_id)
        if lease is None or not lease.is_viewer or not lease.readers:
            return LeaseDecision(lease, out)
        if whep_session is None:
            lease.readers.clear()
        else:
            cameras = [c for c, s in lease.readers.items() if s == whep_session]
            if not cameras:
                return LeaseDecision(lease, out)
            for camera in cameras:
                del lease.readers[camera]
            if lease.readers:
                return LeaseDecision(lease, out)
        lease.bind_deadline = now + self.policy.unbound_lease
        self._settle(now, out)
        return LeaseDecision(lease, out)

    def source_started(
        self,
        session_id: UUID,
        source: CameraProvider,
        publishing: tuple[str, ...] | list[str],
        now: datetime,
    ) -> list[Effect]:
        """The source has media (``STARTING -> LIVE``). Stale reports are ignored."""
        out: list[Effect] = []
        self._advance(now, out)
        if self.state is not SessionState.STARTING or session_id != self.session_id:
            return out
        if source is CameraProvider.RELAY:
            msg = "the source of a relay session cannot be the relay"
            raise ValueError(msg)
        self.state = SessionState.LIVE
        self.start_deadline = None
        self.started_at = now
        self.failures = 0
        self.source = source
        self.publishing = tuple(publishing)
        for camera in self.publishing:
            self.silent_cameras.pop(camera, None)
        out.append(
            EmitEvent(
                self.key,
                "started",
                CAMERA_RELAY_STARTED,
                CameraRelayStarted(
                    relay_session_id=session_id,
                    account_id=self.key.account_id,
                    mower_id=self.key.mower_id,
                    source=source,
                    camera_ids=list(self.publishing),
                    started_at=now,
                    trigger=self.trigger,
                    started_by=self.started_by,
                ),
                now,
            )
        )
        self._settle(now, out)
        return out

    def cameras_published(
        self, session_id: UUID, cameras: Sequence[str], now: datetime
    ) -> list[Effect]:
        """More cameras of the running source deliver (after ``source_started``). Stale
        reports are ignored; no effect."""
        out: list[Effect] = []
        self._advance(now, out)
        if self.state in (SessionState.LIVE, SessionState.DRAINING) and (
            session_id == self.session_id
        ):
            self.publishing = tuple(dict.fromkeys((*self.publishing, *cameras)))
            for camera in cameras:
                self.silent_cameras.pop(camera, None)
        return out

    def source_failed(
        self, session_id: UUID, now: datetime, *, release: bool = True
    ) -> list[Effect]:
        """The source failed (start refused, connection lost). Stale reports are ignored.

        ``release`` false leaves the vendor session alone (no driver for its provider: a direct
        viewer may still use the stream)."""
        out: list[Effect] = []
        self._advance(now, out)
        if self.state in ACTIVE_STATES and session_id == self.session_id:
            self._fail(now, out, release=release)
        return out

    def stop_completed(self, session_id: UUID, now: datetime) -> list[Effect]:
        """The source was left and the stream released: ``STOPPING -> IDLE``."""
        out: list[Effect] = []
        self._advance(now, out)
        if self.state is SessionState.STOPPING and session_id == self.session_id:
            self._reset()
        return out

    def mower_status_changed(self, status: MowerStatus, now: datetime) -> list[Effect]:
        """New mower state: an offline mower (no connection) stops the session; asleep does
        not (a viewer or an error analysis may wake it). A Vision limit, falling asleep
        included, ends routine Vision-only use."""
        out: list[Effect] = []
        self._advance(now, out)
        self.status = status
        if self.state in ACTIVE_STATES and not status.online:
            self._stop(RelayStopReason.MOWER_OFFLINE, now, out)
        else:
            self._settle(now, out)
        return out

    def tick(self, now: datetime) -> list[Effect]:
        """Process every deadline due up to ``now``."""
        out: list[Effect] = []
        self._advance(now, out)
        return out

    def shutdown(self, now: datetime) -> list[Effect]:
        """The relay instance stops: end a running session."""
        out: list[Effect] = []
        self._advance(now, out)
        if self.state in ACTIVE_STATES:
            self._stop(RelayStopReason.SHUTDOWN, now, out)
        return out

    # --- Rules --------------------------------------------------------------------------------

    def _admission(
        self,
        purpose: RelayPurpose,
        now: datetime,
        demand: VisionDemand = VisionDemand.ROUTINE,
    ) -> Refusal | None:
        if self.state is SessionState.STOPPING:
            return Refusal(RelayErrorCode.UNAVAILABLE, "the session is stopping; retry shortly")
        if self.state is SessionState.FAILED:
            wait = (self.retry_at - now).total_seconds() if self.retry_at else 0
            return Refusal(
                RelayErrorCode.UNAVAILABLE, f"the source failed; retry in {max(wait, 0):.0f} s"
            )
        if self.status is not None and not self.status.online:
            return Refusal(RelayErrorCode.UNAVAILABLE, "the mower is offline (no connection to it)")
        if len(self.leases) >= self.policy.max_leases_per_session:
            return Refusal(RelayErrorCode.UNAVAILABLE, "the relay is full for this mower")
        if purpose is RelayPurpose.VISION and not self.has_present_viewer:
            if demand is VisionDemand.ERROR_ANALYSIS:
                return self.policy.error_analysis_refusal(self.status)
            return self.policy.vision_refusal(self.status, self.vision_used_on(_utc_day(now)))
        return None

    def _lease_cameras(self, camera_ids: Sequence[str]) -> tuple[str, ...]:
        requested = tuple(dict.fromkeys(camera_ids))
        offered = tuple(camera.id for camera in self.offered)
        if not requested:
            return offered
        if not offered:
            return requested
        return tuple(camera for camera in requested if camera in offered)

    def _begin(
        self,
        cameras: tuple[str, ...],
        now: datetime,
        purpose: RelayPurpose,
        principal: PrincipalRef,
    ) -> StartVendorSession:
        self.session_id = self._new_id()
        self.relay_id = self._new_path_token()
        self.state = SessionState.STARTING
        self.start_deadline = now + self.policy.start_timeout
        self.cameras = cameras
        self.wanted_since = dict.fromkeys(cameras, now)
        self.session_vision = timedelta(0)
        self.trigger = purpose
        self.started_by = principal
        self.peak_viewers = 0
        return StartVendorSession(self.key, self.session_id, self.relay_id, cameras)

    def _new_lease(
        self,
        cameras: tuple[str, ...],
        purpose: RelayPurpose,
        principal: PrincipalRef,
        now: datetime,
        vision_mode: VisionMode,
    ) -> Lease:
        if purpose is RelayPurpose.VIEWER:
            return Lease(
                lease_id=self._new_id(),
                key=self.key,
                camera_ids=cameras,
                purpose=purpose,
                principal=principal,
                opened_at=now,
                expires_at=now + self.policy.lease_ttl,
                bind_deadline=now + self.policy.unbound_lease,
            )
        cap = (
            self.policy.vision_snapshot_hold
            if vision_mode is VisionMode.SNAPSHOT
            else self.policy.vision_stream_lease
        )
        return Lease(
            lease_id=self._new_id(),
            key=self.key,
            camera_ids=cameras,
            purpose=purpose,
            principal=principal,
            opened_at=now,
            expires_at=now + cap,
            vision_mode=vision_mode,
        )

    def _settle(self, now: datetime, out: list[Effect]) -> None:
        """After the leases or the presence changed: limits, camera set, grace."""
        self._enforce_vision_limits(now, out)
        if self.state not in ACTIVE_STATES:
            return
        if self.leases:
            cameras = tuple(
                dict.fromkeys(c for lease in self.leases.values() for c in lease.camera_ids)
            )
            if cameras != self.cameras:
                self.cameras = cameras
                for camera in cameras:
                    self.wanted_since.setdefault(camera, now)
                out.append(self._update_cameras())
            if self.state is SessionState.DRAINING:
                self.state = SessionState.LIVE
                self.drain_deadline = None
        elif self.state is SessionState.LIVE:
            self.state = SessionState.DRAINING
            self.drain_deadline = now + self.policy.grace

    def _enforce_vision_limits(self, now: datetime, out: list[Effect]) -> None:
        """Routine Vision-only use ends when a limit is reached (falling asleep included);
        error analyses are not ended by a limit. With no lease left the session stops at once
        (the grace is for viewers switching views)."""
        if not self.routine_vision_only:
            return
        refusal = self.policy.vision_refusal(self.status, self.vision_used_on(_utc_day(now)))
        if refusal is None:
            return
        for lease in [lease for lease in self.leases.values() if lease.is_routine_vision]:
            self._close(lease, RelayLeaseCloseReason.EXPIRED, now, out)
        if not self.leases:
            reason = (
                RelayStopReason.BUDGET
                if refusal.code is RelayErrorCode.BUDGET
                else RelayStopReason.MOWER_OFFLINE
            )
            self._stop(reason, now, out)

    def _update_cameras(self) -> UpdateCameras:
        assert self.session_id is not None and self.relay_id is not None  # noqa: S101
        return UpdateCameras(self.key, self.session_id, self.relay_id, self.cameras)

    def _close(
        self, lease: Lease, reason: RelayLeaseCloseReason, at: datetime, out: list[Effect]
    ) -> None:
        del self.leases[lease.lease_id]
        for reader in lease.readers.values():
            out.append(KickViewer(self.key, lease.lease_id, reader))
        assert self.session_id is not None  # noqa: S101 - leases exist only with a session
        out.append(
            EmitEvent(
                self.key,
                "lease_closed",
                CAMERA_RELAY_LEASE_CLOSED,
                CameraRelayLeaseClosed(
                    lease_id=lease.lease_id,
                    relay_session_id=self.session_id,
                    account_id=self.key.account_id,
                    mower_id=self.key.mower_id,
                    purpose=lease.purpose,
                    reason=reason,
                    opened_at=lease.opened_at,
                    closed_at=at,
                ),
                at,
            )
        )

    def _lease_opened(self, lease: Lease) -> EmitEvent:
        assert self.session_id is not None  # noqa: S101
        return EmitEvent(
            self.key,
            "lease_opened",
            CAMERA_RELAY_LEASE_OPENED,
            CameraRelayLeaseOpened(
                lease_id=lease.lease_id,
                relay_session_id=self.session_id,
                account_id=self.key.account_id,
                mower_id=self.key.mower_id,
                purpose=lease.purpose,
                requested_by=lease.principal,
                camera_ids=list(lease.camera_ids),
                opened_at=lease.opened_at,
            ),
            lease.opened_at,
        )

    def _stop(
        self, reason: RelayStopReason, at: datetime, out: list[Effect], *, release: bool = True
    ) -> None:
        """Close every lease, leave the source and (``release``) release the stream:
        ``-> STOPPING``."""
        assert self.session_id is not None and self.relay_id is not None  # noqa: S101
        self._remember_silent(at)
        for lease in list(self.leases.values()):
            self._close(lease, RelayLeaseCloseReason.SESSION_STOPPED, at, out)
        out.append(StopVendorSession(self.key, self.session_id, self.relay_id, release=release))
        out.append(
            EmitEvent(
                self.key,
                "stopped",
                CAMERA_RELAY_STOPPED,
                CameraRelayStopped(
                    relay_session_id=self.session_id,
                    account_id=self.key.account_id,
                    mower_id=self.key.mower_id,
                    reason=reason,
                    started_at=self.started_at,
                    stopped_at=at,
                    vision_seconds=self.session_vision.total_seconds(),
                    trigger=self.trigger,
                    peak_viewers=self.peak_viewers,
                ),
                at,
            )
        )
        self.state = SessionState.STOPPING
        self.start_deadline = None
        self.drain_deadline = None

    def _remember_silent(self, at: datetime) -> None:
        """At the end of a live session: the wanted cameras that never published are silent
        (from the publishers alone: no camera, model or placement is special)."""
        if self.started_at is None:
            return
        for camera in self.cameras:
            if camera in self.publishing:
                self.silent_cameras.pop(camera, None)
            elif camera not in self.silent_cameras and self.camera_available(camera, at) is False:
                self.silent_cameras[camera] = at
        memory = self.policy.silent_camera_memory
        for camera, found in list(self.silent_cameras.items()):
            if at - found >= memory:
                del self.silent_cameras[camera]

    def _fail(self, at: datetime, out: list[Effect], *, release: bool = True) -> None:
        self._stop(RelayStopReason.FAILED, at, out, release=release)
        self.state = SessionState.FAILED
        self.failures += 1
        self.retry_at = at + self.policy.backoff(self.failures)

    def _reset(self) -> None:
        self.state = SessionState.IDLE
        self.session_id = None
        self.relay_id = None
        self.cameras = ()
        self.offered = ()
        self.source = None
        self.trigger = None
        self.started_by = None
        self.peak_viewers = 0
        self.publishing = ()
        self.wanted_since = {}
        self.started_at = None
        self.start_deadline = None
        self.drain_deadline = None
        self.retry_at = None
        self.session_vision = timedelta(0)

    # --- Time ---------------------------------------------------------------------------------

    def _advance(self, now: datetime, out: list[Effect]) -> None:
        """Process every deadline due up to ``now``, each at its own time, in time order."""
        while (due := self._next_due()) is not None and due[0] <= now:
            at, action = due
            self._account(at)
            action(at, out)
        self._account(now)

    def _next_due(self) -> tuple[datetime, Callable[[datetime, list[Effect]], None]] | None:
        candidates: list[tuple[datetime, int, Callable[[datetime, list[Effect]], None]]] = []
        for lease in self.leases.values():
            at, reason = lease.deadline()
            candidates.append((at, 0, self._expire(lease, reason)))
        if self.state is SessionState.STARTING and self.start_deadline is not None:
            candidates.append((self.start_deadline, 1, self._fail))
        if self.state is SessionState.DRAINING and self.drain_deadline is not None:
            candidates.append((self.drain_deadline, 2, self._drained))
        if self.state is SessionState.FAILED and self.retry_at is not None:
            candidates.append((self.retry_at, 3, self._retry))
        if self.routine_vision_only and self._accounted_at is not None:
            used = self.vision_used_on(_utc_day(self._accounted_at))
            remaining = max(self.policy.vision_daily - used, timedelta(0))
            candidates.append((self._accounted_at + remaining, 4, self._enforce_vision_limits))
        if not candidates:
            return None
        at, _, action = min(candidates, key=lambda c: (c[0], c[1]))
        return at, action

    def _expire(
        self, lease: Lease, reason: RelayLeaseCloseReason
    ) -> Callable[[datetime, list[Effect]], None]:
        def expire(at: datetime, out: list[Effect]) -> None:
            self._close(lease, reason, at, out)
            self._settle(at, out)

        return expire

    def _drained(self, at: datetime, out: list[Effect]) -> None:
        self._stop(RelayStopReason.IDLE, at, out)

    def _retry(self, at: datetime, out: list[Effect]) -> None:
        del at, out
        self._reset()

    def _account(self, at: datetime) -> None:
        """Add the Vision-only time since the last call (split at UTC midnight)."""
        start = self._accounted_at
        if start is None or at <= start:
            self._accounted_at = at if start is None else max(start, at)
            return
        if self.vision_only:
            while start < at:
                end = min(at, _next_midnight(start))
                day = _utc_day(start)
                self._vision_by_day[day] = self.vision_used_on(day) + (end - start)
                self.session_vision += end - start
                start = end
            today = _utc_day(at)
            for day in [d for d in self._vision_by_day if d < today - timedelta(days=1)]:
                del self._vision_by_day[day]
        self._accounted_at = at
