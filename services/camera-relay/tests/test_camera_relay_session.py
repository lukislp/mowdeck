"""The relay session's state machine with a fake time: leases, grace, failures, limits."""

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from mowdeck_contracts.v1.camera import CameraProvider
from mowdeck_contracts.v1.relay import (
    CameraRelayLeaseClosed,
    CameraRelayStarted,
    CameraRelayStopped,
    RelayErrorCode,
    RelayLeaseCloseReason,
    RelayPurpose,
    RelayStopReason,
    VisionDemand,
)
from relay_support import ONLINE, T0, at, events, of_type, principal, session

from camera_relay.domain.effects import (
    Effect,
    IssueTicket,
    KickViewer,
    StartVendorSession,
    StopVendorSession,
    UpdateCameras,
)
from camera_relay.domain.models import Lease, SessionState, VisionMode
from camera_relay.domain.policy import MowerStatus, RelayPolicy
from camera_relay.domain.session import RelaySession


def _attach(
    s: RelaySession,
    seconds: float,
    cameras: tuple[str, ...] = ("front_left",),
    purpose: RelayPurpose = RelayPurpose.VIEWER,
    mode: VisionMode = VisionMode.STREAM,
) -> Lease:
    decision = s.attach(
        camera_ids=cameras,
        purpose=purpose,
        principal=principal(),
        now=at(seconds),
        vision_mode=mode,
    )
    assert decision.lease is not None, decision.refusal
    return decision.lease


def _state(s: RelaySession) -> SessionState:
    """The state, read through a call so type narrowing does not carry over."""
    return s.state


def _live(s: RelaySession, seconds: float = 1) -> None:
    assert s.session_id is not None
    s.source_started(s.session_id, CameraProvider.FAKE, s.cameras, at(seconds))
    assert s.state is SessionState.LIVE


def _confirmed_viewer(
    s: RelaySession, seconds: float, cameras: tuple[str, ...] = ("front_left",)
) -> Lease:
    lease = _attach(s, seconds, cameras)
    s.media_confirmed(lease.lease_id, f"whep-{lease.lease_id}", at(seconds))
    return lease


def _closed(effects: list[Effect]) -> list[CameraRelayLeaseClosed]:
    out = []
    for e in events(effects, "lease_closed"):
        assert isinstance(e.payload, CameraRelayLeaseClosed)
        out.append(e.payload)
    return out


def _stopped(effects: list[Effect]) -> CameraRelayStopped:
    (event,) = events(effects, "stopped")
    assert isinstance(event.payload, CameraRelayStopped)
    return event.payload


# --- Reference counting -----------------------------------------------------------------------


def test_first_attach_starts_the_source_and_issues_a_ticket() -> None:
    s = session()
    decision = s.attach(
        camera_ids=["front_left", "front_right"],
        purpose=RelayPurpose.VIEWER,
        principal=principal(),
        now=T0,
    )
    assert decision.lease is not None and decision.refusal is None
    assert s.state is SessionState.STARTING
    (start,) = of_type(decision.effects, StartVendorSession)
    assert start.camera_ids == ("front_left", "front_right")
    assert start.relay_id == s.relay_id and len(start.relay_id) == 32
    (ticket,) = of_type(decision.effects, IssueTicket)
    assert ticket.claims.lease_id == decision.lease.lease_id
    assert ticket.claims.expires_at == T0 + timedelta(seconds=60)
    assert [e.kind for e in events(decision.effects)] == ["lease_opened"]


@pytest.mark.parametrize(
    ("steps", "state", "leases", "cameras"),
    [
        ([("attach", ("a",))], SessionState.LIVE, 1, ("a",)),
        ([("attach", ("a",)), ("attach", ("b",))], SessionState.LIVE, 2, ("a", "b")),
        ([("attach", ("a",)), ("attach", ("a", "b")), ("detach", 1)], SessionState.LIVE, 1, ("a",)),
        ([("attach", ("a",)), ("attach", ("b",)), ("detach", 0)], SessionState.LIVE, 1, ("b",)),
        (
            [("attach", ("a",)), ("attach", ("b",)), ("detach", 0), ("detach", 1)],
            SessionState.DRAINING,
            0,
            ("b",),
        ),
    ],
)
def test_reference_count_and_camera_union(
    steps: list[tuple[str, object]],
    state: SessionState,
    leases: int,
    cameras: tuple[str, ...],
) -> None:
    s = session()
    opened: list[Lease] = []
    starts = 0
    for i, (action, arg) in enumerate(steps):
        if action == "attach":
            assert isinstance(arg, tuple)
            decision = s.attach(
                camera_ids=arg, purpose=RelayPurpose.VIEWER, principal=principal(), now=at(i)
            )
            assert decision.lease is not None
            opened.append(decision.lease)
            starts += len(of_type(decision.effects, StartVendorSession))
            if s.state is SessionState.STARTING:
                _live(s, i)
        else:
            assert isinstance(arg, int)
            s.detach(opened[arg].lease_id, at(i))
    assert starts == 1
    assert (s.state, len(s.leases), s.cameras) == (state, leases, cameras)


def test_camera_set_changes_are_sent_to_the_source() -> None:
    s = session()
    a = _attach(s, 0, ("a",))
    _live(s)
    decision = s.attach(
        camera_ids=["b"], purpose=RelayPurpose.VIEWER, principal=principal(), now=at(2)
    )
    assert [u.camera_ids for u in of_type(decision.effects, UpdateCameras)] == [("a", "b")]
    effects = s.detach(a.lease_id, at(3)).effects
    assert [u.camera_ids for u in of_type(effects, UpdateCameras)] == [("b",)]


# --- Grace --------------------------------------------------------------------------------------


def test_last_viewer_leaving_stops_after_exactly_the_grace() -> None:
    s = session()
    lease = _confirmed_viewer(s, 0)
    _live(s, 1)
    s.detach(lease.lease_id, at(10))
    assert s.state is SessionState.DRAINING
    assert s.tick(at(24.999)) == []
    assert s.state is SessionState.DRAINING
    effects = s.tick(at(25))
    assert _state(s) is SessionState.STOPPING
    (stop,) = of_type(effects, StopVendorSession)
    assert stop.release is True
    stopped = _stopped(effects)
    assert (stopped.reason, stopped.started_at, stopped.stopped_at) == (
        RelayStopReason.IDLE,
        at(1),
        at(25),
    )
    s.stop_completed(stop.session_id, at(25))
    assert _state(s) is SessionState.IDLE and s.relay_id is None


def test_a_late_tick_still_stops_at_the_exact_grace_end() -> None:
    s = session()
    lease = _confirmed_viewer(s, 0)
    _live(s, 1)
    s.detach(lease.lease_id, at(10))
    assert _stopped(s.tick(at(100))).stopped_at == at(25)


@pytest.mark.parametrize("after", [0.001, 7.5, 14.999])
def test_reattach_within_the_grace_keeps_the_source(after: float) -> None:
    s = session()
    lease = _confirmed_viewer(s, 0)
    _live(s, 1)
    relay_id, session_id = s.relay_id, s.session_id
    s.detach(lease.lease_id, at(10))
    decision = s.attach(
        camera_ids=["front_left"],
        purpose=RelayPurpose.VIEWER,
        principal=principal(),
        now=at(10 + after),
    )
    assert decision.lease is not None
    assert of_type(decision.effects, StartVendorSession) == []
    assert of_type(decision.effects, StopVendorSession) == []
    assert (s.state, s.relay_id, s.session_id) == (SessionState.LIVE, relay_id, session_id)


def test_attach_after_the_grace_starts_a_new_session() -> None:
    s = session()
    lease = _confirmed_viewer(s, 0)
    _live(s, 1)
    old = s.relay_id
    s.detach(lease.lease_id, at(10))
    effects = s.tick(at(25))
    (stop,) = of_type(effects, StopVendorSession)
    s.stop_completed(stop.session_id, at(25))
    decision = s.attach(
        camera_ids=["front_left"], purpose=RelayPurpose.VIEWER, principal=principal(), now=at(26)
    )
    (start,) = of_type(decision.effects, StartVendorSession)
    assert start.relay_id != old


def test_attach_while_stopping_is_unavailable() -> None:
    s = session()
    lease = _confirmed_viewer(s, 0)
    _live(s, 1)
    s.detach(lease.lease_id, at(2))
    s.tick(at(17))
    decision = s.attach(
        camera_ids=["a"], purpose=RelayPurpose.VIEWER, principal=principal(), now=at(17)
    )
    assert decision.refusal is not None and decision.refusal.code is RelayErrorCode.UNAVAILABLE


# --- Viewer presence ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("confirm_at", "lose_at", "closed_at", "reason"),
    [
        (None, None, 30, RelayLeaseCloseReason.EXPIRED),  # never bound
        (5, 20, 45, RelayLeaseCloseReason.EXPIRED),  # heartbeat (45 s) before rebinding (50 s)
        (5, 10, 40, RelayLeaseCloseReason.MEDIA_LOST),  # lost, not rebound within 30 s
    ],
)
def test_unconfirmed_and_lost_leases_expire(
    confirm_at: float | None,
    lose_at: float | None,
    closed_at: float,
    reason: RelayLeaseCloseReason,
) -> None:
    s = session()
    lease = _attach(s, 0)
    _live(s, 1)
    if confirm_at is not None:
        s.media_confirmed(lease.lease_id, "whep-1", at(confirm_at))
    if lose_at is not None:
        s.media_lost(lease.lease_id, at(lose_at))
    assert _closed(s.tick(at(closed_at - 0.001))) == []
    (closed,) = _closed(s.tick(at(closed_at)))
    assert (closed.reason, closed.closed_at, closed.opened_at) == (reason, at(closed_at), T0)
    assert s.state is SessionState.DRAINING


def test_heartbeat_lapse_closes_the_lease_and_kicks_the_viewer() -> None:
    s = session()
    lease = _confirmed_viewer(s, 0)
    _live(s, 1)
    s.renew(lease.lease_id, at(15))
    s.renew(lease.lease_id, at(30))
    assert s.tick(at(74.999)) == []
    effects = s.tick(at(75))
    assert [k.whep_session for k in of_type(effects, KickViewer)] == [f"whep-{lease.lease_id}"]
    (closed,) = _closed(effects)
    assert (closed.reason, closed.closed_at) == (RelayLeaseCloseReason.EXPIRED, at(75))
    assert s.renew(lease.lease_id, at(76)).lease is None


def test_reconnect_with_a_new_reader_kicks_the_old_one() -> None:
    s = session()
    lease = _confirmed_viewer(s, 0)
    effects = s.media_confirmed(lease.lease_id, "whep-new", at(2)).effects
    assert [k.whep_session for k in of_type(effects, KickViewer)] == [f"whep-{lease.lease_id}"]


def test_one_reader_per_camera_both_tiles_of_one_lease_stay() -> None:
    s = session()
    lease = _attach(s, 0, ("front_left", "front_right"))
    _live(s, 1)
    left = s.media_confirmed(lease.lease_id, "whep-left", at(2), "front_left").effects
    right = s.media_confirmed(lease.lease_id, "whep-right", at(2), "front_right").effects
    # The right tile's reader does not kick the left one.
    assert of_type(left + right, KickViewer) == []
    assert lease.readers == {"front_left": "whep-left", "front_right": "whep-right"}
    assert lease.present
    # A reconnect of the left tile replaces only the left reader.
    again = s.media_confirmed(lease.lease_id, "whep-left-2", at(3), "front_left").effects
    assert [k.whep_session for k in of_type(again, KickViewer)] == ["whep-left"]
    assert lease.readers == {"front_left": "whep-left-2", "front_right": "whep-right"}
    # Losing one tile keeps the lease present; losing both starts the binding deadline.
    s.media_lost(lease.lease_id, at(4), "whep-right")
    assert lease.present and lease.bind_deadline is None
    s.media_lost(lease.lease_id, at(5), "whep-left-2")
    assert not lease.present and lease.bind_deadline is not None
    # Closing kicks every reader still bound.
    s.media_confirmed(lease.lease_id, "whep-l3", at(6), "front_left")
    s.media_confirmed(lease.lease_id, "whep-r3", at(6), "front_right")
    effects = s.detach(lease.lease_id, at(7)).effects
    assert sorted(k.whep_session for k in of_type(effects, KickViewer)) == ["whep-l3", "whep-r3"]


def test_detach_of_a_bound_viewer_kicks_its_reader() -> None:
    s = session()
    lease = _confirmed_viewer(s, 0)
    effects = s.detach(lease.lease_id, at(1)).effects
    assert len(of_type(effects, KickViewer)) == 1
    assert _closed(effects)[0].reason is RelayLeaseCloseReason.DETACHED


# --- Vision leases ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "renews", "closed_at"),
    [
        (VisionMode.SNAPSHOT, [], 30),
        (VisionMode.SNAPSHOT, [10, 20, 29], 30),  # a snapshot is never extended
        (VisionMode.STREAM, [], 300),
        (VisionMode.STREAM, [100], 400),  # pulling renews the stream lease
    ],
)
def test_vision_lease_never_outlives_its_cap(
    mode: VisionMode, renews: list[float], closed_at: float
) -> None:
    s = session()
    lease = _attach(s, 0, purpose=RelayPurpose.VISION, mode=mode)
    _live(s, 1)
    for seconds in renews:
        assert s.renew(lease.lease_id, at(seconds)).lease is not None
    assert _closed(s.tick(at(closed_at - 0.001))) == []
    (closed,) = _closed(s.tick(at(closed_at)))
    assert (closed.reason, closed.closed_at) == (RelayLeaseCloseReason.EXPIRED, at(closed_at))


def test_vision_lease_ends_with_its_request() -> None:
    s = session()
    lease = _attach(s, 0, purpose=RelayPurpose.VISION, mode=VisionMode.SNAPSHOT)
    _live(s, 1)
    (closed,) = _closed(s.detach(lease.lease_id, at(3)).effects)
    assert closed.reason is RelayLeaseCloseReason.DETACHED
    assert s.state is SessionState.DRAINING


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (None, RelayErrorCode.UNAVAILABLE),
        (MowerStatus(online=False), RelayErrorCode.UNAVAILABLE),
        (MowerStatus(online=True, asleep=True), RelayErrorCode.UNAVAILABLE),
        (MowerStatus(online=True, battery_percent=14.9), RelayErrorCode.BUDGET),
        (MowerStatus(online=True, quota_remaining_percent=9.9), RelayErrorCode.BUDGET),
        (MowerStatus(online=True, battery_percent=14.9, charging=True), None),
        (MowerStatus(online=True, battery_percent=15, quota_remaining_percent=10), None),
    ],
)
def test_vision_admission(status: MowerStatus | None, code: RelayErrorCode | None) -> None:
    s = session(status=status)
    decision = s.attach(
        camera_ids=["a"], purpose=RelayPurpose.VISION, principal=principal(), now=T0
    )
    assert (decision.refusal.code if decision.refusal else None) is code
    assert (decision.lease is None) is (code is not None)


@pytest.mark.parametrize(
    ("status", "admitted", "warning"),
    [
        (None, True, False),
        (MowerStatus(online=False), False, False),
        (MowerStatus(online=True, asleep=True), True, False),  # a viewer may wake it
        (MowerStatus(online=True, battery_percent=5), True, True),
        (MowerStatus(online=True, quota_remaining_percent=1), True, True),
        (ONLINE, True, False),
    ],
)
def test_viewers_are_never_refused_for_a_limit(
    status: MowerStatus | None, admitted: bool, warning: bool
) -> None:
    s = session(status=status)
    decision = s.attach(
        camera_ids=["a"], purpose=RelayPurpose.VIEWER, principal=principal(), now=T0
    )
    assert (decision.lease is not None, decision.budget_warning) == (admitted, warning)
    if not admitted:
        assert decision.refusal is not None
        assert decision.refusal.code is RelayErrorCode.UNAVAILABLE  # never wake, never budget


def test_vision_with_a_present_viewer_is_admitted_below_the_limits() -> None:
    s = session(status=MowerStatus(online=True, battery_percent=5))
    viewer = s.attach(camera_ids=["a"], purpose=RelayPurpose.VIEWER, principal=principal(), now=T0)
    assert viewer.lease is not None
    s.media_confirmed(viewer.lease.lease_id, "w", T0)
    vision = s.attach(
        camera_ids=["a"], purpose=RelayPurpose.VISION, principal=principal(), now=at(1)
    )
    assert vision.refusal is None


def _run_vision_stream(s: RelaySession, start: float, seconds: float) -> Lease:
    lease = _attach(s, start, purpose=RelayPurpose.VISION)
    if s.state is SessionState.STARTING:
        _live(s, start)
    t = start
    while t + 100 < start + seconds:
        t += 100
        s.renew(lease.lease_id, at(t))
    return lease


def test_daily_vision_minutes_end_vision_only_use_and_then_refuse() -> None:
    s = session()
    _run_vision_stream(s, 0, 1150)
    effects = s.tick(at(1300))
    (closed,) = _closed(effects)
    assert (closed.reason, closed.closed_at) == (RelayLeaseCloseReason.EXPIRED, at(1200))
    stopped = _stopped(effects)
    assert (stopped.reason, stopped.stopped_at, stopped.vision_seconds) == (
        RelayStopReason.BUDGET,
        at(1200),
        1200,
    )
    (stop,) = of_type(effects, StopVendorSession)
    s.stop_completed(stop.session_id, at(1300))
    vision = s.attach(
        camera_ids=["a"], purpose=RelayPurpose.VISION, principal=principal(), now=at(1301)
    )
    assert vision.refusal is not None and vision.refusal.code is RelayErrorCode.BUDGET
    viewer = s.attach(
        camera_ids=["a"], purpose=RelayPurpose.VIEWER, principal=principal(), now=at(1302)
    )
    assert viewer.refusal is None


def test_vision_minutes_count_only_without_a_present_viewer() -> None:
    s = session()
    viewer = _confirmed_viewer(s, 0)
    _live(s, 0)
    vision = _attach(s, 0, purpose=RelayPurpose.VISION)
    s.renew(viewer.lease_id, at(40))
    s.renew(viewer.lease_id, at(80))
    s.tick(at(100))
    assert s.vision_used_on(T0.date()) == timedelta(0)
    s.detach(viewer.lease_id, at(100))
    s.tick(at(150))
    assert s.vision_used_on(T0.date()) == timedelta(seconds=50)
    effects = s.detach(vision.lease_id, at(160)).effects
    assert _closed(effects)[0].reason is RelayLeaseCloseReason.DETACHED
    assert _stopped(s.tick(at(175))).vision_seconds == 60


def test_daily_vision_minutes_reset_at_utc_midnight() -> None:
    late = T0.replace(hour=23, minute=55)
    s = RelaySession(session().key, status=ONLINE)
    lease = s.attach(camera_ids=["a"], purpose=RelayPurpose.VISION, principal=principal(), now=late)
    assert lease.lease is not None and s.session_id is not None
    s.source_started(s.session_id, CameraProvider.FAKE, ["a"], late)
    s.renew(lease.lease.lease_id, late + timedelta(minutes=4))
    s.tick(late + timedelta(minutes=8))
    assert s.vision_used_on(late.date()) == timedelta(minutes=5)
    assert s.vision_used_on(late.date() + timedelta(days=1)) == timedelta(minutes=3)


def test_a_battery_drop_ends_vision_only_use() -> None:
    s = session()
    _run_vision_stream(s, 0, 10)
    effects = s.mower_status_changed(MowerStatus(online=True, battery_percent=10), at(20))
    assert _closed(effects)[0].reason is RelayLeaseCloseReason.EXPIRED
    assert _stopped(effects).reason is RelayStopReason.BUDGET


def test_a_battery_drop_keeps_viewers() -> None:
    s = session()
    _confirmed_viewer(s, 0)
    _live(s, 1)
    assert s.mower_status_changed(MowerStatus(online=True, battery_percent=10), at(20)) == []
    assert s.state is SessionState.LIVE


# --- Failures -----------------------------------------------------------------------------------


def test_start_timeout_fails_with_a_doubling_backoff_capped_at_60_seconds() -> None:
    s = session()
    t = 0.0
    waits: list[float] = []
    session_ids: set[UUID] = set()
    for _ in range(6):
        lease = _attach(s, t)
        assert s.state is SessionState.STARTING and s.session_id is not None
        session_ids.add(s.session_id)
        assert s.tick(at(t + 29.999)) == []
        effects = s.tick(at(t + 30))
        assert _state(s) is SessionState.FAILED
        assert _stopped(effects).reason is RelayStopReason.FAILED
        assert _stopped(effects).started_at is None
        assert _closed(effects)[0].lease_id == lease.lease_id
        assert len(of_type(effects, StopVendorSession)) == 1
        assert s.retry_at is not None
        wait = (s.retry_at - at(t + 30)).total_seconds()
        waits.append(wait)
        refused = s.attach(
            camera_ids=["a"],
            purpose=RelayPurpose.VIEWER,
            principal=principal(),
            now=at(t + 30 + wait - 0.001),
        )
        assert refused.refusal is not None and refused.refusal.code is RelayErrorCode.UNAVAILABLE
        t += 30 + wait
    assert waits == [5, 10, 20, 40, 60, 60]
    assert len(session_ids) == 6


def test_going_live_resets_the_backoff() -> None:
    s = session()
    _attach(s, 0)
    s.tick(at(30))
    _attach(s, 35)
    _live(s, 36)
    assert s.failures == 0
    assert s.policy.backoff(1) == timedelta(seconds=5)


def test_source_failure_while_live_closes_the_leases() -> None:
    s = session()
    lease = _confirmed_viewer(s, 0)
    _live(s, 1)
    assert s.session_id is not None
    effects = s.source_failed(s.session_id, at(20))
    assert s.state is SessionState.FAILED
    assert _closed(effects)[0].reason is RelayLeaseCloseReason.SESSION_STOPPED
    assert [k.lease_id for k in of_type(effects, KickViewer)] == [lease.lease_id]
    stopped = _stopped(effects)
    assert (stopped.reason, stopped.started_at) == (RelayStopReason.FAILED, at(1))


def test_stale_source_reports_are_ignored() -> None:
    s = session()
    _attach(s, 0)
    other = UUID(int=1)
    assert s.source_started(other, CameraProvider.FAKE, ["a"], at(1)) == []
    assert s.source_failed(other, at(1)) == []
    assert s.state is SessionState.STARTING


# --- Mower state --------------------------------------------------------------------------------


def test_mower_offline_while_live_stops_the_session() -> None:
    s = session()
    lease = _confirmed_viewer(s, 0)
    _live(s, 1)
    effects = s.mower_status_changed(MowerStatus(online=False), at(5))
    assert s.state is SessionState.STOPPING
    (closed,) = _closed(effects)
    assert (closed.lease_id, closed.reason) == (
        lease.lease_id,
        RelayLeaseCloseReason.SESSION_STOPPED,
    )
    assert _stopped(effects).reason is RelayStopReason.MOWER_OFFLINE
    assert of_type(effects, StopVendorSession)[0].release is True
    assert len(of_type(effects, KickViewer)) == 1
    (stop,) = of_type(effects, StopVendorSession)
    s.stop_completed(stop.session_id, at(5))
    refused = s.attach(
        camera_ids=["a"], purpose=RelayPurpose.VIEWER, principal=principal(), now=at(6)
    )
    assert refused.refusal is not None and refused.refusal.code is RelayErrorCode.UNAVAILABLE


def test_shutdown_stops_a_running_session() -> None:
    s = session()
    _confirmed_viewer(s, 0)
    _live(s, 1)
    assert _stopped(s.shutdown(at(2))).reason is RelayStopReason.SHUTDOWN


# --- Events -------------------------------------------------------------------------------------


def test_started_event_names_the_source_and_the_publishing_cameras() -> None:
    s = session()
    _attach(s, 0, ("front_left", "front_right", "rear"))
    assert s.session_id is not None
    effects = s.source_started(
        s.session_id, CameraProvider.AGORA, ["front_left", "front_right"], at(2)
    )
    (event,) = events(effects, "started")
    assert isinstance(event.payload, CameraRelayStarted)
    assert (event.payload.source, event.payload.camera_ids, event.payload.started_at) == (
        CameraProvider.AGORA,
        ["front_left", "front_right"],
        at(2),
    )
    assert event.event_type == "camera.relay.started"
    assert event.payload.relay_session_id == s.session_id


def test_the_relay_is_never_its_own_source() -> None:
    s = session()
    _attach(s, 0)
    assert s.session_id is not None
    with pytest.raises(ValueError, match="relay"):
        s.source_started(s.session_id, CameraProvider.RELAY, ["a"], at(1))


def test_lease_limit_per_session() -> None:
    s = RelaySession(session().key, status=ONLINE, policy=RelayPolicy(max_leases_per_session=2))
    _attach(s, 0)
    _attach(s, 0)
    refused = s.attach(
        camera_ids=["a"], purpose=RelayPurpose.VIEWER, principal=principal(), now=at(1)
    )
    assert refused.refusal is not None and "full" in refused.refusal.detail


def test_an_idle_session_without_vision_minutes_is_dormant() -> None:
    s = session()
    assert s.is_dormant(T0)
    lease = _attach(s, 0)
    assert not s.is_dormant(T0)
    s.detach(lease.lease_id, at(1))
    assert not s.is_dormant(at(1))


# --- Camera availability ------------------------------------------------------------------------


def test_a_listed_camera_that_never_publishes_is_reported_unavailable_after_the_settle() -> None:
    s = session()
    _attach(s, 0, ("front_left", "front_right", "rear"))
    assert s.session_id is not None
    assert s.camera_available("rear", at(0)) is None  # starting: not known yet
    s.source_started(s.session_id, CameraProvider.AGORA, ["front_left"], at(5))
    assert s.state is SessionState.LIVE  # one camera is enough
    s.cameras_published(s.session_id, ["front_right"], at(6))
    assert [s.camera_available(c, at(10)) for c in ("front_left", "front_right", "rear")] == [
        True,
        True,
        None,  # may still come: within the settle time after going live
    ]
    assert s.camera_available("rear", at(20)) is False
    assert s.camera_available("front_right", at(20)) is True


def test_stale_publish_reports_are_ignored() -> None:
    s = session()
    _attach(s, 0, ("front_left", "front_right"))
    assert s.session_id is not None
    assert s.cameras_published(s.session_id, ["front_right"], at(1)) == []  # still starting
    assert s.publishing == ()
    _live(s, 2)
    s.cameras_published(uuid4(), ["rear"], at(3))
    assert "rear" not in s.publishing


def test_a_camera_asked_for_later_gets_its_own_settle_time() -> None:
    s = session()
    _attach(s, 0, ("front_left",))
    _live(s, 1)
    _attach(s, 30, ("front_left", "front_right"))  # the second camera joins the running source
    assert s.camera_available("front_right", at(31)) is None
    assert s.camera_available("front_right", at(46)) is False


# --- Owner decisions 2026-10-18: who may wake a sleeping mower --------------------------------


def _vision(
    s: RelaySession, seconds: float, demand: VisionDemand
) -> tuple[Lease | None, RelayErrorCode | None]:
    decision = s.attach(
        camera_ids=["front_left"],
        purpose=RelayPurpose.VISION,
        principal=principal(),
        now=at(seconds),
        vision_mode=VisionMode.SNAPSHOT,
        vision_demand=demand,
    )
    return decision.lease, decision.refusal.code if decision.refusal else None


ASLEEP = MowerStatus(online=True, asleep=True, battery_percent=80)


@pytest.mark.parametrize(
    ("status", "error_analysis", "routine"),
    [
        (ASLEEP, None, RelayErrorCode.UNAVAILABLE),
        (MowerStatus(online=True, battery_percent=5), None, RelayErrorCode.BUDGET),
        (MowerStatus(online=True, quota_remaining_percent=1), None, RelayErrorCode.BUDGET),
        (MowerStatus(online=False), RelayErrorCode.UNAVAILABLE, RelayErrorCode.UNAVAILABLE),
        (None, RelayErrorCode.UNAVAILABLE, RelayErrorCode.UNAVAILABLE),
        (ONLINE, None, None),
    ],
)
def test_error_analysis_may_wake_and_skips_the_budget_routine_may_not(
    status: MowerStatus | None,
    error_analysis: RelayErrorCode | None,
    routine: RelayErrorCode | None,
) -> None:
    assert _vision(session(status=status), 0, VisionDemand.ERROR_ANALYSIS)[1] is error_analysis
    assert _vision(session(status=status), 0, VisionDemand.ROUTINE)[1] is routine


def test_used_up_vision_minutes_refuse_routine_but_not_error_analysis() -> None:
    s = session()
    s._vision_by_day[T0.date()] = timedelta(minutes=20)
    assert _vision(s, 0, VisionDemand.ROUTINE)[1] is RelayErrorCode.BUDGET
    lease, refusal = _vision(s, 0, VisionDemand.ERROR_ANALYSIS)
    assert refusal is None and lease is not None
    _live(s, 1)
    # No limit ends it: it lives until its own cap (30 s for a snapshot), and ticking past
    # the used-up budget neither closes it early nor loops.
    s.tick(at(20))
    assert lease.lease_id in s.leases
    (closed,) = _closed(s.tick(at(31)))
    assert closed.reason is RelayLeaseCloseReason.EXPIRED


def test_asleep_does_not_stop_a_session_with_viewers_but_offline_does() -> None:
    s = session()
    viewer = _confirmed_viewer(s, 0)
    _live(s, 1)
    assert not of_type(s.mower_status_changed(ASLEEP, at(2)), StopVendorSession)
    assert s.state is SessionState.LIVE and viewer.lease_id in s.leases
    _lease, refusal = _vision(s, 3, VisionDemand.ROUTINE)
    assert refusal is None, "a present viewer holds the session: Vision may use it"
    stopped = s.mower_status_changed(MowerStatus(online=False), at(4))
    assert of_type(stopped, StopVendorSession)
    assert _stopped(stopped).reason is RelayStopReason.MOWER_OFFLINE
    assert s.leases == {}


def test_routine_vision_alone_ends_when_the_mower_falls_asleep() -> None:
    s = session()
    _vision(s, 0, VisionDemand.ROUTINE)
    _live(s, 1)
    effects = s.mower_status_changed(ASLEEP, at(2))
    (stop,) = of_type(effects, StopVendorSession)
    assert stop.release is True
    assert _stopped(effects).reason is RelayStopReason.MOWER_OFFLINE
    assert s.leases == {}


def test_an_error_analysis_keeps_running_while_the_mower_sleeps() -> None:
    s = session(status=ASLEEP)
    lease, refusal = _vision(s, 0, VisionDemand.ERROR_ANALYSIS)
    assert refusal is None and lease is not None
    _live(s, 1)
    assert not of_type(s.mower_status_changed(ASLEEP, at(2)), StopVendorSession)
    assert lease.lease_id in s.leases
