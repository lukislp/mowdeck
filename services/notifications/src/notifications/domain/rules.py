"""The notification rules: pure functions over a :class:`MowerWatch`.

Every function takes the watch (None for a mower seen for the first time), the input and the
current time, and returns the new watch plus the derived events. Nothing here reads a clock or
does I/O, so the rules are tested with fixed timestamps.

=====================  ===========================================================================
Kind                   Fires when
=====================  ===========================================================================
``job_started``        the activity becomes ``mowing`` while no job runs
``mowing_finished``    a running job reaches ``docked`` or ``charging`` after at least
                       ``mowing_min_seconds`` (shorter jobs end silently)
``mower_stuck``        the activity is ``error`` (once per error until it clears; a lift raises
                       ``mower_lifted`` instead), or the position did not change by
                       ``stuck_move_meters`` for ``stuck_no_move_seconds`` while ``mowing``
``battery_low``        the battery falls below ``battery_low_percent`` while not charging; it fires
                       again only after the battery was back at ``battery_rearm_percent``
``mower_offline``      no state for ``offline_after_seconds``, a state with activity ``offline``,
                       or ``mower.disconnected`` without a reconnect for
                       ``disconnect_grace_seconds``
``mower_online``       the first state or connect after a notified ``mower_offline``
``mower_lifted``       the activity is ``error`` and the state indicates a lift
                       (``raw["lifted"] is True`` or the error code is in ``lift_error_codes``),
                       once per occurrence until the error clears
``mower_left_dock_``   the activity changes from ``docked``/``charging`` to anything but
``unexpectedly``       ``docked``, ``charging``, ``offline`` or ``unknown`` while no command was
                       seen from ``dock_command_window_seconds`` before the departure up to
                       ``dock_command_grace_seconds`` after the state; the departure is held and
                       alarms only when a state at least ``dock_confirm_seconds`` later still
                       shows the mower away without a command
``geofence_exit``      ``geofence_confirm_states`` consecutive positions (``rtk_fix`` not
                       ``none``) outside the mapped areas, paths and dock buffered by
                       ``geofence_margin_meters``; not while a manual drive frame was seen within
                       ``dock_command_window_seconds``; re-armed once the mower is inside again
``blade_maintenance``  Telemetry's ``telemetry.blade_hours.threshold`` (one per crossing)
``vision_proposal``    Vision's ``vision.proposal.created`` (one per proposal, ``can_drive``)
``vision_auto_``        the same for an automatic analysis (``trigger`` ``error``, ADR 0035)
``proposal``
``firmware_update_``   ``firmware_info.update_state`` is ``available`` with a version the mower
``available``          was not notified for yet (once per offered version, ADR 0038)
``firmware_update_``   ``firmware_info`` went from ``installing`` to ``idle`` (the new version
``finished``           is installed) ...
``firmware_update_``   ... or to ``failed`` (with the mower's reason)
``failed``
=====================  ===========================================================================

A kind is sent at most once per ``cooldown_seconds`` and mower (the theft alarms: once per
``alarm_cooldown_seconds``); a suppressed notification still changes the watch (a suppressed
offline is not followed by an online).

The command order race: the gateway executes a start command and then publishes the new state,
but the command (published before execution) and the state are consumed by different consumers
in any order. A command whose ``issued_at`` lies within the window before the departure, or up to
the grace after the evaluated state, explains it; and a departure without a known command is only
remembered (``left_dock_at``), so a command consumed a little later still explains it before the
confirming state arrives.
"""

import math
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from mowdeck_contracts.v1 import (
    TRACKING_ENDED,
    TRACKING_MOVED,
    TRACKING_STALE,
    TRACKING_STARTED,
    BladeHoursThreshold,
    FirmwareInfo,
    MowerActivity,
    MowerState,
    NotificationKind,
    RtkFixStatus,
    TrackingEvent,
    VisionProposalCreated,
    VisionProposalState,
    VisionProposalUpdated,
    VisionTrigger,
)

from notifications.domain.geofence import Fence, distance_outside
from notifications.domain.models import (
    ALARM_KINDS,
    MaintenanceWindow,
    MowerWatch,
    NotificationEvent,
    RuleConfig,
)

_JOB_END = frozenset({MowerActivity.DOCKED, MowerActivity.CHARGING})
_AT_DOCK = frozenset({MowerActivity.DOCKED.value, MowerActivity.CHARGING.value})
_NOT_AWAY = frozenset(
    {
        MowerActivity.DOCKED,
        MowerActivity.CHARGING,
        MowerActivity.OFFLINE,
        MowerActivity.UNKNOWN,
    }
)
_EARTH_RADIUS_M = 6_371_000.0

Outcome = tuple[MowerWatch, list[NotificationEvent]]


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great circle distance in metres (haversine)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def _minutes(delta: timedelta) -> int:
    return max(0, round(delta.total_seconds() / 60))


def _emit(
    watch: MowerWatch,
    event: NotificationEvent,
    now: datetime,
    config: RuleConfig,
    events: list[NotificationEvent],
) -> tuple[MowerWatch, bool]:
    """Append ``event`` unless its kind is cooling down; returns the watch and whether it went."""
    last = watch.last_sent.get(event.kind.value)
    cooldown = (
        config.alarm_cooldown_seconds if event.kind in ALARM_KINDS else config.cooldown_seconds
    )
    if last is not None and now - last < timedelta(seconds=cooldown):
        return watch, False
    events.append(event)
    return watch.evolve(last_sent={**watch.last_sent, event.kind.value: now}), True


def _go_offline(
    watch: MowerWatch,
    since: datetime,
    reason: str,
    now: datetime,
    config: RuleConfig,
    events: list[NotificationEvent],
    window: MaintenanceWindow | None = None,
) -> MowerWatch:
    if window is not None and window.holds(since, now, config):
        # A restart of the deploy broke the link: no offline now, and so no online later. The
        # sweep announces it if the mower is still offline when the window and its grace are over.
        return watch.evolve(
            offline=True,
            offline_since=since,
            offline_notified=False,
            offline_held=True,
            disconnected_at=None,
        )
    event = NotificationEvent.of(
        NotificationKind.MOWER_OFFLINE,
        watch.mower_id,
        watch.account_id,
        now,
        since.isoformat(),
        variant=reason,
        reason=reason,
        minutes=_minutes(now - since),
    )
    watch, sent = _emit(watch, event, now, config, events)
    return watch.evolve(
        offline=True,
        offline_since=since,
        offline_notified=sent,
        offline_held=False,
        disconnected_at=None,
    )


def release_held_offline(
    watch: MowerWatch, now: datetime, config: RuleConfig, window: MaintenanceWindow | None
) -> Outcome:
    """Announce an offline that was held back once the window and its grace are over.

    The mower is still offline then (a come-back clears the hold), so this is a real outage.
    """
    events: list[NotificationEvent] = []
    if not (watch.offline and watch.offline_held):
        return watch, events
    if window is not None and now <= window.held_until(config.maintenance_grace_seconds):
        return watch, events
    since = watch.offline_since or now
    event = NotificationEvent.of(
        NotificationKind.MOWER_OFFLINE,
        watch.mower_id,
        watch.account_id,
        now,
        since.isoformat(),
        variant="no_state",
        reason="no_state",
        minutes=_minutes(now - since),
    )
    watch, sent = _emit(watch, event, now, config, events)
    return watch.evolve(offline_notified=sent, offline_held=False), events


def _come_online(
    watch: MowerWatch, now: datetime, config: RuleConfig, events: list[NotificationEvent]
) -> MowerWatch:
    if not watch.offline:
        return watch
    if watch.offline_notified:
        since = watch.offline_since or now
        event = NotificationEvent.of(
            NotificationKind.MOWER_ONLINE,
            watch.mower_id,
            watch.account_id,
            now,
            since.isoformat(),
            minutes=_minutes(now - since),
        )
        watch, _ = _emit(watch, event, now, config, events)
    return watch.evolve(
        offline=False, offline_since=None, offline_notified=False, offline_held=False
    )


def is_lift(state: MowerState, config: RuleConfig) -> bool:
    """True when the state reports a lift (``raw["lifted"]`` or a configured error code)."""
    if state.activity is not MowerActivity.ERROR:
        return False
    if state.raw.get("lifted") is True:
        return True
    return state.error_code is not None and state.error_code in config.lift_error_codes


def _where(mower_id: UUID, state: MowerState) -> tuple[str, dict[str, str]]:
    """The map link and the ``lat``/``lon`` parameters (6 decimals) of a state."""
    position = state.position
    if position is None:
        return f"/mowers/{mower_id}/map", {}
    lat, lon = f"{position.lat:.6f}", f"{position.lon:.6f}"
    return f"/mowers/{mower_id}/map?lat={lat}&lon={lon}", {"lat": lat, "lon": lon}


def _lifted(
    w: MowerWatch,
    state: MowerState,
    key: str,
    now: datetime,
    config: RuleConfig,
    events: list[NotificationEvent],
) -> MowerWatch:
    link, coords = _where(w.mower_id, state)
    event = NotificationEvent.of(
        NotificationKind.MOWER_LIFTED,
        w.mower_id,
        w.account_id,
        state.observed_at,
        f"{key}:{state.observed_at.isoformat()}",
        variant=None if coords else "no_position",
        link=link,
        error_code=state.error_code or "unknown",
        **coords,
    )
    w, _ = _emit(w, event, now, config, events)
    return w


def _explained(w: MowerWatch, since: datetime, observed: datetime, config: RuleConfig) -> bool:
    """True when a command explains a departure at ``since``, evaluated with the state observed
    at ``observed``."""
    last = w.last_command_at
    if last is None:
        return False
    window = timedelta(seconds=config.dock_command_window_seconds)
    grace = timedelta(seconds=config.dock_command_grace_seconds)
    return since - window <= last <= observed + grace


def _dock_rule(
    w: MowerWatch,
    previous: str | None,
    state: MowerState,
    now: datetime,
    config: RuleConfig,
    events: list[NotificationEvent],
) -> MowerWatch:
    activity, observed = state.activity, state.observed_at
    if activity.value in _AT_DOCK:
        return w.evolve(left_dock_at=None) if w.left_dock_at is not None else w
    since = w.left_dock_at
    if since is None:
        if previous not in _AT_DOCK or activity in _NOT_AWAY:
            return w
        if _explained(w, observed, observed, config):
            return w
        since = observed
        w = w.evolve(left_dock_at=since)  # hold: a late command may still explain it
    if activity in _NOT_AWAY:
        return w
    if _explained(w, since, observed, config):
        return w.evolve(left_dock_at=None)
    if observed - since < timedelta(seconds=config.dock_confirm_seconds):
        return w
    link, coords = _where(w.mower_id, state)
    event = NotificationEvent.of(
        NotificationKind.MOWER_LEFT_DOCK_UNEXPECTEDLY,
        w.mower_id,
        w.account_id,
        observed,
        since.isoformat(),
        variant=None if coords else "no_position",
        link=link,
        activity=activity.value,
        **coords,
    )
    w, _ = _emit(w, event, now, config, events)
    return w.evolve(left_dock_at=None)


def _geofence_rule(
    w: MowerWatch,
    state: MowerState,
    fence: Fence | None,
    now: datetime,
    config: RuleConfig,
    events: list[NotificationEvent],
) -> MowerWatch:
    position = state.position
    if fence is None or position is None or state.rtk_fix is RtkFixStatus.NONE:
        return w
    observed = state.observed_at
    window = timedelta(seconds=config.dock_command_window_seconds)
    if w.last_drive_at is not None and w.last_drive_at >= observed - window:
        return w.evolve(outside_count=0)  # someone drives it by hand
    outside = distance_outside(fence, position.lat, position.lon, config.geofence_margin_meters)
    if outside <= 0:
        if w.outside_count or w.geofence_notified:
            return w.evolve(outside_count=0, geofence_notified=False)
        return w
    w = w.evolve(outside_count=w.outside_count + 1)
    if w.outside_count < config.geofence_confirm_states or w.geofence_notified:
        return w
    link, coords = _where(w.mower_id, state)
    event = NotificationEvent.of(
        NotificationKind.GEOFENCE_EXIT,
        w.mower_id,
        w.account_id,
        observed,
        observed.isoformat(),
        link=link,
        meters=max(1, round(outside)),
        **coords,
    )
    w, _ = _emit(w, event, now, config, events)
    return w.evolve(geofence_notified=True)


def _firmware_rule(
    w: MowerWatch,
    info: FirmwareInfo | None,
    observed: datetime,
    now: datetime,
    config: RuleConfig,
    events: list[NotificationEvent],
) -> MowerWatch:
    """Firmware (ADR 0038): an offer once per version, the end of an update once."""
    if info is None:
        return w
    previous = w.firmware_state
    offered = info.available if info.update_state == "available" else None
    if offered and offered != w.firmware_offered:
        event = NotificationEvent.of(
            NotificationKind.FIRMWARE_UPDATE_AVAILABLE,
            w.mower_id,
            w.account_id,
            observed,
            offered,
            link=f"/mowers/{w.mower_id}/settings",
            version=offered,
            installed=info.installed or "",
        )
        w, _ = _emit(w, event, now, config, events)
        w = w.evolve(firmware_offered=offered)
    if previous == "installing" and info.update_state == "idle":
        event = NotificationEvent.of(
            NotificationKind.FIRMWARE_UPDATE_FINISHED,
            w.mower_id,
            w.account_id,
            observed,
            f"{info.installed or ''}:{observed.isoformat()}",
            link=f"/mowers/{w.mower_id}/settings",
            version=info.installed or w.firmware_offered or "",
        )
        w, _ = _emit(w, event, now, config, events)
        w = w.evolve(firmware_offered=None)
    elif previous == "installing" and info.update_state == "failed":
        event = NotificationEvent.of(
            NotificationKind.FIRMWARE_UPDATE_FAILED,
            w.mower_id,
            w.account_id,
            observed,
            observed.isoformat(),
            link=f"/mowers/{w.mower_id}/settings",
            version=w.firmware_offered or "",
            reason=info.failure_reason or "",
        )
        w, _ = _emit(w, event, now, config, events)
    return w.evolve(firmware_state=info.update_state)


def new_watch(mower_id: UUID, account_id: UUID, now: datetime) -> MowerWatch:
    """The watch of a mower seen for the first time."""
    return MowerWatch(mower_id=mower_id, account_id=account_id, last_seen_at=now)


def observe_state(
    watch: MowerWatch | None,
    state: MowerState,
    now: datetime,
    config: RuleConfig,
    fence: Fence | None = None,
    window: MaintenanceWindow | None = None,
) -> Outcome:
    """Apply one state of the mower (``fence``: its geofence, when a map is known; ``window``:
    the deploy maintenance window, if one was open)."""
    events: list[NotificationEvent] = []
    if watch is None or watch.account_id != state.account_id:
        # First sight (or the mower changed accounts): remember the state, notify nothing, so a
        # restart of this service does not repeat old news.
        first = new_watch(state.mower_id, state.account_id, now).evolve(
            last_observed_at=state.observed_at,
            activity=state.activity.value,
            battery_percent=state.battery_percent,
            battery_armed=state.battery_percent >= config.battery_low_percent or state.charging,
            job_started_at=state.observed_at if state.activity is MowerActivity.MOWING else None,
            error_notified=(state.error_code or "error")
            if state.activity is MowerActivity.ERROR
            else None,
            firmware_state=state.firmware_info.update_state if state.firmware_info else None,
            firmware_offered=state.firmware_info.available
            if state.firmware_info and state.firmware_info.update_state == "available"
            else None,
        )
        return first, events
    if watch.last_observed_at is not None and state.observed_at < watch.last_observed_at:
        return watch.evolve(last_seen_at=now), events  # out of order: proves contact only

    observed = state.observed_at
    activity = state.activity
    previous = watch.activity
    w = watch.evolve(last_seen_at=now, last_observed_at=observed, disconnected_at=None)

    if activity is MowerActivity.OFFLINE:
        if not w.offline:
            w = _go_offline(w, observed, "reported", now, config, events, window)
        return w.evolve(activity=activity.value, battery_percent=state.battery_percent), events
    w = _come_online(w, now, config, events)

    # Jobs: started on the first mowing state, finished at the dock.
    if activity is MowerActivity.MOWING and w.job_started_at is None:
        w = w.evolve(job_started_at=observed)
        started = NotificationEvent.of(
            NotificationKind.JOB_STARTED,
            w.mower_id,
            w.account_id,
            observed,
            observed.isoformat(),
            battery=state.battery_percent,
        )
        w, _ = _emit(w, started, now, config, events)
    elif (activity in _JOB_END or state.charging) and w.job_started_at is not None:
        duration = observed - w.job_started_at
        if duration >= timedelta(seconds=config.mowing_min_seconds):
            finished = NotificationEvent.of(
                NotificationKind.MOWING_FINISHED,
                w.mower_id,
                w.account_id,
                observed,
                w.job_started_at.isoformat(),
                minutes=_minutes(duration),
                battery=state.battery_percent,
            )
            w, _ = _emit(w, finished, now, config, events)
        w = w.evolve(job_started_at=None)
    elif activity is MowerActivity.IDLE:
        w = w.evolve(job_started_at=None)  # stopped in the lawn: the job ended without a dock

    # Errors: once per error until the mower leaves the error state.
    if activity is MowerActivity.ERROR:
        key = state.error_code or "error"
        if w.error_notified != key and is_lift(state, config):
            # the lift alarm replaces mower_stuck for this error
            w = _lifted(w, state, key, now, config, events).evolve(error_notified=key)
        elif w.error_notified != key:
            stuck = NotificationEvent.of(
                NotificationKind.MOWER_STUCK,
                w.mower_id,
                w.account_id,
                observed,
                f"error:{key}:{observed.isoformat()}",
                variant="error",
                reason="error",
                error_code=state.error_code or "unknown",
                error_message=state.error_message or "",
            )
            w, _ = _emit(w, stuck, now, config, events)
            w = w.evolve(error_notified=key)
    elif w.error_notified is not None:
        w = w.evolve(error_notified=None)

    # Theft alarms: an unexplained departure from the dock, a position outside the fence.
    w = _dock_rule(w, previous, state, now, config, events)
    w = _geofence_rule(w, state, fence, now, config, events)

    # Firmware: a new offer, the end of an update.
    w = _firmware_rule(w, state.firmware_info, observed, now, config, events)

    # Standing still while mowing.
    position = state.position
    if (
        activity is MowerActivity.MOWING
        and position is not None
        and config.stuck_no_move_seconds > 0
    ):
        moved = (
            w.anchor_lat is None
            or w.anchor_lon is None
            or w.anchor_since is None
            or distance_m(w.anchor_lat, w.anchor_lon, position.lat, position.lon)
            >= config.stuck_move_meters
        )
        if moved:
            w = w.evolve(
                anchor_lat=position.lat,
                anchor_lon=position.lon,
                anchor_since=observed,
                no_move_notified=False,
            )
        elif (
            not w.no_move_notified
            and w.anchor_since is not None
            and observed - w.anchor_since >= timedelta(seconds=config.stuck_no_move_seconds)
        ):
            still = NotificationEvent.of(
                NotificationKind.MOWER_STUCK,
                w.mower_id,
                w.account_id,
                observed,
                f"still:{w.anchor_since.isoformat()}",
                variant="no_movement",
                reason="no_movement",
                minutes=_minutes(observed - w.anchor_since),
            )
            w, _ = _emit(w, still, now, config, events)
            w = w.evolve(no_move_notified=True)
    elif activity is not MowerActivity.PAUSED:
        w = w.evolve(anchor_lat=None, anchor_lon=None, anchor_since=None, no_move_notified=False)

    # Battery with hysteresis: once per discharge.
    battery = state.battery_percent
    if w.battery_armed and battery < config.battery_low_percent and not state.charging:
        low = NotificationEvent.of(
            NotificationKind.BATTERY_LOW,
            w.mower_id,
            w.account_id,
            observed,
            observed.isoformat(),
            battery=battery,
        )
        w, _ = _emit(w, low, now, config, events)
        w = w.evolve(battery_armed=False)
    elif not w.battery_armed and battery >= config.battery_rearm_percent:
        w = w.evolve(battery_armed=True)

    return w.evolve(activity=activity.value, battery_percent=battery), events


def observe_connection(
    watch: MowerWatch | None,
    mower_id: UUID,
    account_id: UUID,
    *,
    connected: bool,
    at: datetime,
    now: datetime,
    config: RuleConfig,
    window: MaintenanceWindow | None = None,
) -> Outcome:
    """Apply ``mower.connected`` (``connected=True``) or ``mower.disconnected``."""
    events: list[NotificationEvent] = []
    w = watch if watch is not None and watch.account_id == account_id else None
    if w is None:
        w = new_watch(mower_id, account_id, now)
    if connected:
        w = w.evolve(disconnected_at=None, last_seen_at=now)
        return _come_online(w, now, config, events), events
    if w.offline:
        return w, events
    if config.disconnect_grace_seconds <= 0:
        return _go_offline(w, at, "disconnected", now, config, events, window), events
    if w.disconnected_at is None:
        w = w.evolve(disconnected_at=at)
    return w, events


def offline_due(watch: MowerWatch, now: datetime, config: RuleConfig) -> bool:
    """True when :func:`check_offline` would mark the mower offline at ``now``."""
    if watch.offline:
        return False
    if watch.disconnected_at is not None and now - watch.disconnected_at >= timedelta(
        seconds=config.disconnect_grace_seconds
    ):
        return True
    return now - watch.last_seen_at >= timedelta(seconds=config.offline_after_seconds)


def check_offline(
    watch: MowerWatch,
    now: datetime,
    config: RuleConfig,
    window: MaintenanceWindow | None = None,
) -> Outcome:
    """The timer rule: no state for too long, or a disconnect without a reconnect."""
    events: list[NotificationEvent] = []
    if not offline_due(watch, now, config):
        return watch, events
    grace = timedelta(seconds=config.disconnect_grace_seconds)
    if watch.disconnected_at is not None and now - watch.disconnected_at >= grace:
        return _go_offline(
            watch, watch.disconnected_at, "disconnected", now, config, events, window
        ), events
    return _go_offline(watch, watch.last_seen_at, "no_state", now, config, events, window), events


def observe_command(watch: MowerWatch, issued_at: datetime, *, drive: bool) -> MowerWatch:
    """Remember a command (``drive``: a manual drive frame); older or repeated ones change
    nothing, so a redelivered command is harmless."""
    w = watch
    if w.last_command_at is None or issued_at > w.last_command_at:
        w = w.evolve(last_command_at=issued_at)
    if drive and (w.last_drive_at is None or issued_at > w.last_drive_at):
        w = w.evolve(last_drive_at=issued_at)
    return w


def observe_blade_hours(
    watch: MowerWatch | None, payload: BladeHoursThreshold, now: datetime, config: RuleConfig
) -> tuple[MowerWatch | None, list[NotificationEvent]]:
    """Telemetry's blade hours crossed the threshold: one ``blade_maintenance`` per crossing
    (the normal cooldown applies when the mower has a watch). Telemetry sends the blade
    counter it shows and the one effective threshold (set in MowDeck, else the mower's own
    warning, else the default), so the reminder names the same numbers as the web and HA."""
    event = NotificationEvent.of(
        NotificationKind.BLADE_MAINTENANCE,
        payload.mower_id,
        payload.account_id,
        payload.crossed_at,
        payload.crossed_at.isoformat(),
        hours=round(payload.blade_hours_since_change),
        threshold=round(payload.threshold_hours),
    )
    if watch is None or watch.account_id != payload.account_id:
        return watch, [event]
    events: list[NotificationEvent] = []
    w, _ = _emit(watch, event, now, config, events)
    return w, events


def tracking_event(event_type: str, payload: TrackingEvent) -> NotificationEvent:
    """The notification of a theft tracking event (ADR 0058). Telemetry already limits how
    often it publishes them (one move notice per five minutes, one stale notice per outage),
    so there is no cooldown; the occurrence (session, kind, time) makes the inbox item unique.
    The notification opens the tracking view, where "false alarm" ends the session."""
    kind = {
        TRACKING_STARTED: NotificationKind.TRACKING_STARTED,
        TRACKING_MOVED: NotificationKind.TRACKING_MOVED,
        TRACKING_STALE: NotificationKind.TRACKING_STALE,
        TRACKING_ENDED: NotificationKind.TRACKING_ENDED,
    }[event_type]
    has_position = payload.lat is not None and payload.lon is not None
    params: dict[str, Any] = {
        "lat": f"{payload.lat:.5f}" if payload.lat is not None else "",
        "lon": f"{payload.lon:.5f}" if payload.lon is not None else "",
        "meters": round(payload.distance_m or 0),
        "minutes": payload.stale_minutes or 0,
        "points": payload.track_points or 0,
        "trigger": payload.trigger,
    }
    if kind is NotificationKind.TRACKING_ENDED:
        variant: str | None = payload.end_reason or "stopped"
    elif has_position:
        variant = None
    else:
        variant = "no_position"
    return NotificationEvent.of(
        kind,
        payload.mower_id,
        payload.account_id,
        payload.at,
        f"{payload.session_id}:{payload.at.isoformat()}",
        variant=variant,
        link=f"/mowers/{payload.mower_id}/tracking",
        **params,
    )


def vision_no_photo_event(payload: VisionProposalUpdated) -> NotificationEvent | None:
    """``vision_auto_proposal`` for an automatic analysis that got no camera photo (state
    ``failed``, trigger ``error``, ``photo_problem`` set): nothing was analysed or driven, the
    body names the likely cause (weak Wi-Fi or mobile signal with its dBm, offline, no frame)
    and asks the owner to start the analysis by hand once the connection is better. None for
    any other update."""
    if (
        payload.trigger is not VisionTrigger.ERROR
        or payload.state is not VisionProposalState.FAILED
        or payload.photo_problem is None
    ):
        return None
    event = NotificationEvent.of(
        NotificationKind.VISION_AUTO_PROPOSAL,
        payload.mower_id,
        payload.account_id,
        payload.updated_at,
        str(payload.proposal_id),
        variant=f"no_photo_{payload.photo_problem.value}",
        link=payload.link,
        relation="can_drive",
        error_code=payload.error_code or "",
        signal_dbm=payload.signal_dbm if payload.signal_dbm is not None else "",
    )
    return replace(event, title_key=f"{NotificationKind.VISION_AUTO_PROPOSAL.value}.title_no_photo")


def vision_proposal_event(payload: VisionProposalCreated, minutes: int) -> NotificationEvent:
    """``vision_proposal`` for a proposal of AI assist, ``vision_auto_proposal`` when the mower's
    error started it automatically (ADR 0035, with ``error_code``): recipients need
    ``can_drive``, the notification opens the proposal (``payload.link``), the body varies by
    task, or is the return to dock or resume body when the proposal is the dock command or
    continues the paused job; a choice body when the model's low-confidence recommendation is
    offered next to the dock fallback (``alternative_action``)."""
    automatic = payload.trigger is VisionTrigger.ERROR
    variant = (
        payload.action if payload.action in ("return_to_dock", "resume") else payload.task.value
    )
    if payload.alternative_action in ("resume", "drive"):
        # The model's low-confidence recommendation next to the dock fallback: a choice.
        variant = f"choice_{payload.alternative_action}"
    return NotificationEvent.of(
        NotificationKind.VISION_AUTO_PROPOSAL if automatic else NotificationKind.VISION_PROPOSAL,
        payload.mower_id,
        payload.account_id,
        payload.created_at,
        str(payload.proposal_id),
        variant=variant,
        link=payload.link,
        relation="can_drive",
        steps=payload.steps,
        confidence=round(payload.confidence * 100),
        minutes=minutes,
        error_code=payload.error_code or "",
    )
