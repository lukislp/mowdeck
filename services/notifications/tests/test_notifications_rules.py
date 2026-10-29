"""The rules engine: every kind, hysteresis, cooldown and the offline timer (manual times)."""

from datetime import timedelta
from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import (
    BladeHoursThreshold,
    FirmwareInfo,
    GeoReference,
    MowerActivity,
    MowerErrorCategory,
    NotificationKind,
    NotificationSeverity,
    RtkFixStatus,
    StartMow,
)
from mowdeck_control import SimulatedMower, SimulationConfig, default_map
from mowdeck_control.geo import to_wgs84
from mowdeck_control.testing import ManualClock as SimClock
from notifications_support import T0, make_state, seconds

from notifications.domain.geofence import Fence, fence_distance, segment_distance
from notifications.domain.models import MowerWatch, NotificationEvent, RuleConfig
from notifications.domain.rules import (
    check_offline,
    distance_m,
    is_lift,
    observe_blade_hours,
    observe_command,
    observe_connection,
    observe_state,
    offline_due,
)

CONFIG = RuleConfig(
    mowing_min_seconds=120,
    stuck_no_move_seconds=300,
    battery_low_percent=20,
    battery_rearm_percent=30,
    offline_after_seconds=120,
    disconnect_grace_seconds=30,
    cooldown_seconds=300,
)
MOWER, ACCOUNT = uuid4(), uuid4()


class Run:
    """Feeds states into the rules and collects the events.

    With ``auto_command`` (the default) a command is recorded right before every state, so
    the dock departure alarm stays quiet in tests about other rules.
    """

    def __init__(
        self,
        config: RuleConfig = CONFIG,
        *,
        auto_command: bool = True,
        fence: Fence | None = None,
        only: set[str] | None = None,
    ) -> None:
        self.config = config
        self.watch: MowerWatch | None = None
        self.events: list[NotificationEvent] = []
        self.t = 0.0
        self.auto_command = auto_command
        self.fence = fence
        self.only = only

    def state(self, activity: MowerActivity, *, after: float = 10, **kwargs: object) -> list[str]:
        self.t += after
        at = T0 + seconds(self.t)
        if self.auto_command and self.watch is not None:
            self.watch = observe_command(self.watch, at, drive=False)
        state = make_state(MOWER, ACCOUNT, at=at, activity=activity, **kwargs)  # type: ignore[arg-type]
        self.watch, events = observe_state(self.watch, state, at, self.config, self.fence)
        self.events.extend(events)
        return [e.kind.value for e in events if self.only is None or e.kind.value in self.only]

    def command(self, *, after: float = 0, drive: bool = False) -> None:
        """Record a command issued ``after`` seconds after the current time."""
        assert self.watch is not None
        self.watch = observe_command(self.watch, T0 + seconds(self.t + after), drive=drive)

    def link(self, *, connected: bool, after: float = 1) -> list[str]:
        self.t += after
        at = T0 + seconds(self.t)
        self.watch, events = observe_connection(
            self.watch, MOWER, ACCOUNT, connected=connected, at=at, now=at, config=self.config
        )
        self.events.extend(events)
        return [e.kind.value for e in events]

    def tick(self, after: float) -> list[str]:
        self.t += after
        assert self.watch is not None
        self.watch, events = check_offline(self.watch, T0 + seconds(self.t), self.config)
        self.events.extend(events)
        return [e.kind.value for e in events]


def test_notifications_rules_first_state_notifies_nothing() -> None:
    run = Run()
    assert run.state(MowerActivity.ERROR, error_code="E1", battery=5) == []
    assert run.watch is not None
    assert run.watch.error_notified == "E1" and not run.watch.battery_armed
    assert run.state(MowerActivity.ERROR, error_code="E1", battery=5) == []


def test_notifications_rules_job_started_and_mowing_finished() -> None:
    run = Run()
    run.state(MowerActivity.DOCKED)
    assert run.state(MowerActivity.MOWING) == ["job_started"]
    assert run.state(MowerActivity.MOWING, after=60) == []
    assert run.state(MowerActivity.PAUSED, after=30) == []
    assert run.state(MowerActivity.MOWING, after=30) == []  # resumed: same job
    assert run.state(MowerActivity.RETURNING, after=60) == []
    assert run.state(MowerActivity.CHARGING, after=60, battery=40) == ["mowing_finished"]
    finished = run.events[-1]
    assert finished.params == {"minutes": 4, "battery": 40}
    assert finished.severity is NotificationSeverity.INFO
    assert finished.title_key == "mowing_finished.title"
    assert finished.dedupe_key == f"mowing_finished:{MOWER}:{(T0 + seconds(20)).isoformat()}"
    assert run.state(MowerActivity.CHARGING) == []  # a heartbeat repeats nothing


def test_notifications_rules_short_jobs_and_idle_end_silently() -> None:
    run = Run(RuleConfig(cooldown_seconds=0))
    run.state(MowerActivity.DOCKED)
    run.state(MowerActivity.MOWING)
    assert run.state(MowerActivity.DOCKED, after=60) == []  # 60 s < 120 s
    assert run.state(MowerActivity.MOWING) == ["job_started"]
    assert run.state(MowerActivity.IDLE, after=600) == []
    assert run.watch is not None and run.watch.job_started_at is None
    assert run.state(MowerActivity.DOCKED) == []
    # charging flag ends a job too
    run.state(MowerActivity.MOWING)
    assert run.state(MowerActivity.IDLE, after=300, charging=True) == ["mowing_finished"]


def test_notifications_rules_error_fires_once_per_error() -> None:
    run = Run(RuleConfig(cooldown_seconds=0))
    run.state(MowerActivity.MOWING)
    kinds = run.state(MowerActivity.ERROR, error_code="E_LIFT", error_message="Lifted")
    assert kinds == ["mower_stuck"]
    event = run.events[-1]
    assert event.body_key == "mower_stuck.body_error"
    assert (
        event.params["error_code"] == "E_LIFT" and event.severity is NotificationSeverity.CRITICAL
    )
    assert run.state(MowerActivity.ERROR, error_code="E_LIFT") == []
    assert run.state(MowerActivity.ERROR, error_code="E_TILT") == ["mower_stuck"]
    assert run.state(MowerActivity.MOWING) == []
    assert run.state(MowerActivity.ERROR) == ["mower_stuck"]
    assert run.events[-1].params["error_code"] == "unknown"


def test_notifications_rules_no_movement_while_mowing() -> None:
    run = Run()
    run.state(MowerActivity.DOCKED)
    run.state(MowerActivity.MOWING, position=(48.0, 11.0))
    assert run.state(MowerActivity.MOWING, after=200, position=(48.0, 11.000001)) == []
    assert run.state(MowerActivity.MOWING, after=100, position=(48.0, 11.0)) == ["mower_stuck"]
    assert run.events[-1].body_key == "mower_stuck.body_no_movement"
    assert run.events[-1].params["minutes"] == 5
    assert run.state(MowerActivity.MOWING, after=600, position=(48.0, 11.0)) == []  # once
    # moving again re-arms; a pause keeps the anchor
    assert run.state(MowerActivity.MOWING, after=10, position=(48.001, 11.0)) == []
    assert run.state(MowerActivity.PAUSED, after=10, position=(48.001, 11.0)) == []
    assert run.watch is not None and run.watch.anchor_since is not None
    assert run.state(MowerActivity.DOCKED, after=10) == ["mowing_finished"]
    assert run.watch.anchor_since is None


def test_notifications_rules_no_movement_disabled() -> None:
    run = Run(RuleConfig(stuck_no_move_seconds=0))
    run.state(MowerActivity.MOWING, position=(48.0, 11.0))
    assert run.state(MowerActivity.MOWING, after=3600, position=(48.0, 11.0)) == []


def test_notifications_rules_battery_hysteresis_once_per_discharge() -> None:
    run = Run(RuleConfig(cooldown_seconds=0))
    run.state(MowerActivity.MOWING, battery=50)
    assert run.state(MowerActivity.MOWING, battery=21) == []
    assert run.state(MowerActivity.MOWING, battery=19) == ["battery_low"]
    assert run.events[-1].params == {"battery": 19}
    assert run.state(MowerActivity.MOWING, battery=18) == []
    assert run.state(MowerActivity.MOWING, battery=22) == []
    assert run.state(MowerActivity.MOWING, battery=19) == []  # not re-armed below 30 %
    assert run.state(MowerActivity.CHARGING, battery=29, charging=True) == []
    assert run.state(MowerActivity.CHARGING, battery=30, charging=True) == []  # re-armed
    assert run.state(MowerActivity.IDLE, battery=15, charging=True) == []  # charging
    assert run.state(MowerActivity.IDLE, battery=15) == ["battery_low"]


def test_notifications_rules_cooldown_per_kind() -> None:
    run = Run(RuleConfig(cooldown_seconds=300, mowing_min_seconds=0))
    run.state(MowerActivity.DOCKED)
    assert run.state(MowerActivity.MOWING) == ["job_started"]
    assert run.state(MowerActivity.DOCKED, after=10) == ["mowing_finished"]
    assert run.state(MowerActivity.MOWING, after=10) == []  # job_started cooling down
    assert run.state(MowerActivity.DOCKED, after=10) == []
    assert run.state(MowerActivity.MOWING, after=300) == ["job_started"]


def test_notifications_rules_offline_without_states_and_online_again() -> None:
    run = Run()
    run.state(MowerActivity.DOCKED)
    assert offline_due(run.watch, T0 + seconds(run.t + 119), CONFIG) is False  # type: ignore[arg-type]
    assert run.tick(119) == []
    assert run.tick(1) == ["mower_offline"]
    offline = run.events[-1]
    assert offline.body_key == "mower_offline.body_no_state"
    assert offline.params["minutes"] == 2 and offline.severity is NotificationSeverity.WARNING
    assert run.tick(600) == []  # once
    assert run.state(MowerActivity.DOCKED, after=60) == ["mower_online"]
    assert run.events[-1].params["minutes"] == 13
    assert run.watch is not None and not run.watch.offline


def test_notifications_rules_disconnect_grace_and_reconnect() -> None:
    run = Run()
    run.state(MowerActivity.DOCKED)
    assert run.link(connected=False) == []
    assert run.tick(10) == []
    assert run.link(connected=True, after=5) == []  # back within the grace: silent
    assert run.watch is not None and run.watch.disconnected_at is None
    assert run.link(connected=False) == []
    assert run.link(connected=False, after=5) == []  # the first disconnect time counts
    assert run.tick(25) == ["mower_offline"]
    assert run.events[-1].body_key == "mower_offline.body_disconnected"
    assert run.link(connected=False) == []  # already offline
    assert run.link(connected=True, after=30) == ["mower_online"]


def test_notifications_rules_disconnect_without_grace_and_unknown_mower() -> None:
    run = Run(RuleConfig(disconnect_grace_seconds=0))
    assert run.link(connected=False) == ["mower_offline"]  # first contact is a disconnect
    assert run.link(connected=True) == ["mower_online"]


def test_notifications_rules_reported_offline_state() -> None:
    run = Run()
    run.state(MowerActivity.MOWING)
    assert run.state(MowerActivity.OFFLINE) == ["mower_offline"]
    assert run.events[-1].body_key == "mower_offline.body_reported"
    assert run.state(MowerActivity.OFFLINE) == []
    assert run.state(MowerActivity.MOWING) == ["mower_online"]


def test_notifications_rules_suppressed_offline_has_no_online() -> None:
    run = Run(RuleConfig(cooldown_seconds=3600, offline_after_seconds=60))
    run.state(MowerActivity.DOCKED)
    assert run.tick(60) == ["mower_offline"]
    assert run.state(MowerActivity.DOCKED) == ["mower_online"]
    assert run.tick(60) == []  # offline again within the cooldown: suppressed
    assert run.watch is not None and run.watch.offline and not run.watch.offline_notified
    assert run.state(MowerActivity.DOCKED) == []


def test_notifications_rules_out_of_order_and_account_change() -> None:
    run = Run()
    run.state(MowerActivity.MOWING, battery=50)
    assert run.watch is not None
    before = run.watch
    old = make_state(MOWER, ACCOUNT, at=T0, activity=MowerActivity.DOCKED, battery=5)
    later = T0 + timedelta(hours=1)
    watch, events = observe_state(run.watch, old, later, CONFIG)
    assert events == [] and watch.activity == "mowing" and watch.last_seen_at == later
    moved = make_state(MOWER, uuid4(), at=T0 + timedelta(hours=2), activity=MowerActivity.DOCKED)
    watch, events = observe_state(before, moved, later, CONFIG)
    assert events == [] and watch.account_id == moved.account_id
    other, events = observe_connection(
        before, MOWER, uuid4(), connected=True, at=later, now=later, config=CONFIG
    )
    assert events == [] and other.account_id != ACCOUNT


def test_notifications_rules_watch_json_round_trip() -> None:
    run = Run(RuleConfig(cooldown_seconds=0))
    run.state(MowerActivity.MOWING, position=(48.0, 11.0), battery=50)
    run.state(MowerActivity.MOWING, position=(48.0, 11.0), battery=10)
    run.link(connected=False)
    assert run.watch is not None
    data = run.watch.to_json()
    assert MowerWatch.from_json(MOWER, ACCOUNT, data) == run.watch
    assert "battery_low" in data["last_sent"]


def test_notifications_rules_distance() -> None:
    assert distance_m(48.0, 11.0, 48.0, 11.0) == 0
    assert 110 < distance_m(48.0, 11.0, 48.001, 11.0) < 112


def test_notifications_rules_event_keys() -> None:
    event = NotificationEvent.of(NotificationKind.BATTERY_LOW, MOWER, ACCOUNT, T0, "x", battery=1)
    assert (event.title_key, event.body_key) == ("battery_low.title", "battery_low.body")
    variant = NotificationEvent.of(
        NotificationKind.MOWER_OFFLINE, MOWER, ACCOUNT, T0, "y", variant="no_state"
    )
    assert variant.body_key == "mower_offline.body_no_state"


# --- theft alarm and blade maintenance ---------------------------------------------------------

THEFT = {"mower_lifted", "mower_left_dock_unexpectedly", "geofence_exit"}
ALARMS = RuleConfig(cooldown_seconds=0, alarm_cooldown_seconds=600, dock_confirm_seconds=20)
SIM_MAP = default_map()
SIM_FENCE = Fence.from_map(SIM_MAP)


def at_local(x: float, y: float) -> tuple[float, float]:
    """A local point of the simulator's map as ``(lat, lon)``."""
    return to_wgs84(SIM_MAP, x, y)


LIFT = MowerErrorCategory.LIFT_TILT


def test_notifications_theft_lift_by_simulator_code_raw_flag_and_cooldown() -> None:
    run = Run(ALARMS)
    run.state(MowerActivity.MOWING, position=(48.1, 11.5))
    # the simulator's lift: code ``lifted`` with its lexicon category ``lift_tilt``
    kinds = run.state(
        MowerActivity.ERROR, error_code="lifted", error_category=LIFT, position=(48.1234567, 11.5)
    )
    assert kinds == ["mower_lifted"]  # no mower_stuck for the same error
    lifted = run.events[-1]
    assert lifted.severity is NotificationSeverity.CRITICAL
    assert lifted.body_key == "mower_lifted.body"
    assert lifted.params["lat"] == "48.123457" and lifted.params["lon"] == "11.500000"
    assert lifted.link == f"/mowers/{MOWER}/map?lat=48.123457&lon=11.500000"
    assert lifted.silence_key == f"mower_lifted:{MOWER}"
    same = run.state(MowerActivity.ERROR, error_code="lifted", error_category=LIFT)
    assert same == []  # same occurrence
    assert run.state(MowerActivity.PAUSED) == []  # cleared
    # back within the alarm cooldown: suppressed, and still no mower_stuck
    assert run.state(MowerActivity.ERROR, error_code="X9", raw={"lifted": True}) == []
    assert run.state(MowerActivity.PAUSED, after=600) == []
    kinds = run.state(MowerActivity.ERROR, error_code="X9", raw={"lifted": True})
    assert kinds == ["mower_lifted"]
    assert run.events[-1].body_key == "mower_lifted.body_no_position"
    assert run.events[-1].link == f"/mowers/{MOWER}/map"
    assert run.state(MowerActivity.ERROR, error_code="E42") == ["mower_stuck"]  # other errors


def test_notifications_theft_lift_by_configured_numeric_code() -> None:
    run = Run(RuleConfig(cooldown_seconds=0, lift_error_codes=frozenset({"77"})))
    run.state(MowerActivity.MOWING)
    assert run.state(MowerActivity.ERROR, error_code="1004") == ["mower_stuck"]
    assert run.state(MowerActivity.ERROR, error_code="77") == ["mower_lifted"]
    default = Run(ALARMS)
    default.state(MowerActivity.MOWING)
    # no vendor codes in the core: a bare code without category or flag is no lift
    assert default.state(MowerActivity.ERROR, error_code="2801") == ["mower_stuck"]
    assert default.state(MowerActivity.PAUSED) == []
    assert default.state(MowerActivity.ERROR, error_code="lifted") == ["mower_stuck"]
    assert default.state(MowerActivity.PAUSED) == []
    assert default.state(MowerActivity.ERROR, raw={"lifted": "yes"}) == ["mower_stuck"]
    moving = make_state(MOWER, ACCOUNT, activity=MowerActivity.MOWING, raw={"lifted": True})
    assert is_lift(moving, ALARMS) is False  # only an error state is a lift


@pytest.mark.parametrize("code", ["1004", "1030", "2502", "2801", "11131", "11133", "-2801"])
def test_notifications_theft_lift_for_mammotion_codes_without_a_code_list(code: str) -> None:
    """The six Mammotion lift and tilt codes left the default code list: the adapter reports
    each with ``error_category`` ``lift_tilt`` and ``raw.lifted`` (its own tests check that), and
    either signal alone raises the alarm with the defaults."""
    assert RuleConfig().lift_error_codes == frozenset()
    for signal in ({"error_category": LIFT}, {"raw": {"lifted": True}}):
        run = Run(ALARMS)
        run.state(MowerActivity.MOWING)
        assert run.state(MowerActivity.ERROR, error_code=code, **signal) == ["mower_lifted"]


def test_notifications_theft_lift_by_lexicon_category() -> None:
    """A vendor without a code list: its lexicon's category ``lift_tilt`` raises the alarm
    (ADR 0067); configured codes keep working, an empty category list turns it off."""
    run = Run(ALARMS)
    run.state(MowerActivity.MOWING)
    lifted = run.state(
        MowerActivity.ERROR, error_code="H-37", error_category=MowerErrorCategory.LIFT_TILT
    )
    assert lifted == ["mower_lifted"]
    assert run.state(MowerActivity.PAUSED) == []
    stuck = run.state(
        MowerActivity.ERROR, error_code="H-12", error_category=MowerErrorCategory.TRAPPED
    )
    assert stuck == ["mower_stuck"]
    assert run.events[-1].params["error_connection"] == "simulated"
    assert run.events[-1].params["error_provider"] == ""  # no cloud provider for the kind
    assert run.events[-1].params["error_category"] == "trapped"
    codes_only = Run(RuleConfig(cooldown_seconds=0, lift_categories=frozenset()))
    codes_only.state(MowerActivity.MOWING)
    assert codes_only.state(
        MowerActivity.ERROR, error_code="H-37", error_category=MowerErrorCategory.LIFT_TILT
    ) == ["mower_stuck"]


def test_notifications_theft_left_dock_without_command_alarms_after_confirm() -> None:
    run = Run(ALARMS, auto_command=False, only=THEFT)
    run.state(MowerActivity.CHARGING, charging=True)
    assert run.state(MowerActivity.MOWING, position=(48.1, 11.5)) == []  # held
    assert run.watch is not None and run.watch.left_dock_at == T0 + seconds(20)
    assert run.state(MowerActivity.MOWING, position=(48.1, 11.5)) == []  # 10 s < 20 s
    kinds = run.state(MowerActivity.IDLE, position=(48.1, 11.5), after=15)
    assert kinds == ["mower_left_dock_unexpectedly"]
    event = run.events[-1]
    assert event.dedupe_key.endswith((T0 + seconds(20)).isoformat())
    assert event.params["activity"] == "idle" and event.params["lat"] == "48.100000"
    assert event.link is not None and event.link.startswith(f"/mowers/{MOWER}/map?lat=")
    assert run.watch is not None and run.watch.left_dock_at is None
    assert run.state(MowerActivity.IDLE, after=30) == []
    # without a position, and with the confirmation off
    now = Run(RuleConfig(dock_confirm_seconds=0), auto_command=False, only=THEFT)
    now.state(MowerActivity.DOCKED)
    assert now.state(MowerActivity.RETURNING) == ["mower_left_dock_unexpectedly"]
    assert now.events[-1].body_key == "mower_left_dock_unexpectedly.body_no_position"


def test_notifications_theft_left_dock_recent_command_or_return_is_silent() -> None:
    run = Run(ALARMS, auto_command=False, only=THEFT)
    run.state(MowerActivity.DOCKED)
    run.command(after=-300)  # 5 min before the departure
    assert run.state(MowerActivity.MOWING) == []
    assert run.watch is not None and run.watch.left_dock_at is None
    assert run.state(MowerActivity.MOWING, after=60) == []
    # a command older than the window does not explain the next departure
    assert run.state(MowerActivity.DOCKED, after=60) == []
    assert run.state(MowerActivity.MOWING, after=600) == []
    assert run.state(MowerActivity.MOWING, after=30) == ["mower_left_dock_unexpectedly"]
    # back at the dock before the confirmation, or unknown/offline: nothing
    back = Run(ALARMS, auto_command=False, only=THEFT)
    back.state(MowerActivity.DOCKED)
    assert back.state(MowerActivity.MOWING) == []
    assert back.state(MowerActivity.UNKNOWN, after=30) == []
    assert back.state(MowerActivity.CHARGING, after=30) == []
    assert back.watch is not None and back.watch.left_dock_at is None
    assert back.state(MowerActivity.UNKNOWN) == []
    assert back.state(MowerActivity.CHARGING) == []
    assert back.state(MowerActivity.OFFLINE) == []
    assert back.watch is not None and back.watch.left_dock_at is None


def test_notifications_theft_left_dock_command_consumed_after_the_state() -> None:
    run = Run(ALARMS, auto_command=False, only=THEFT)
    run.state(MowerActivity.DOCKED)
    assert run.state(MowerActivity.MOWING) == []  # the command is not known yet: held
    run.command(after=-2)  # issued before the state, consumed after it
    assert run.state(MowerActivity.MOWING, after=30) == []
    assert run.watch is not None and run.watch.left_dock_at is None
    skewed = Run(ALARMS, auto_command=False, only=THEFT)
    skewed.state(MowerActivity.DOCKED)
    assert skewed.state(MowerActivity.MOWING) == []
    skewed.command(after=5)  # issued slightly after the state (clock skew)
    assert skewed.state(MowerActivity.MOWING, after=30) == []
    assert skewed.watch is not None and skewed.watch.left_dock_at is None
    # issued at the moment of the departure: explains it at once
    direct = Run(ALARMS, auto_command=False, only=THEFT)
    direct.state(MowerActivity.DOCKED)
    direct.command(after=12)
    assert direct.state(MowerActivity.MOWING) == []
    assert direct.watch is not None and direct.watch.left_dock_at is None


def test_notifications_theft_redelivered_command_is_harmless() -> None:
    run = Run(ALARMS, auto_command=False, only=THEFT)
    run.state(MowerActivity.DOCKED)
    run.command()
    assert run.watch is not None
    first = run.watch
    assert observe_command(first, first.last_command_at or T0, drive=False) == first
    assert observe_command(first, T0 - seconds(60), drive=False) == first  # older
    drive = observe_command(first, T0 + seconds(10), drive=True)
    assert drive.last_drive_at == T0 + seconds(10) == drive.last_command_at
    assert observe_command(drive, T0 + seconds(5), drive=True) == drive
    assert run.state(MowerActivity.MOWING) == []


def test_notifications_theft_geofence_inside_path_dock_and_margin() -> None:
    assert SIM_FENCE is not None
    run = Run(ALARMS, fence=SIM_FENCE)
    for x, y in ((5, 4), (1, 9.5), (-1, 1), (-4, 4), (5, 20)):
        assert run.state(MowerActivity.MOWING, position=at_local(x, y)) == [], (x, y)
        assert run.state(MowerActivity.MOWING, position=at_local(x, y)) == [], (x, y)
    assert run.watch is not None and run.watch.outside_count == 0


def test_notifications_theft_geofence_needs_two_states_and_re_arms() -> None:
    config = RuleConfig(cooldown_seconds=0, alarm_cooldown_seconds=0)
    run = Run(config, fence=SIM_FENCE)
    run.state(MowerActivity.MOWING, position=at_local(5, 4))
    far = at_local(32, 4)  # 20 m right of "front", 15 m beyond the margin
    assert run.state(MowerActivity.MOWING, position=far) == []
    assert run.state(MowerActivity.MOWING, position=far) == ["geofence_exit"]
    event = run.events[-1]
    assert event.params["meters"] == 15 and event.severity is NotificationSeverity.CRITICAL
    assert event.link == f"/mowers/{MOWER}/map?lat={far[0]:.6f}&lon={far[1]:.6f}"
    assert run.state(MowerActivity.MOWING, position=far) == []  # once
    assert run.state(MowerActivity.MOWING) == []  # no position: no change
    assert run.state(MowerActivity.MOWING, position=at_local(5, 4)) == []  # re-armed
    assert run.state(MowerActivity.MOWING, position=far) == []
    assert run.state(MowerActivity.MOWING, position=far) == ["geofence_exit"]
    # the alarm cooldown also applies to the geofence
    cooled = Run(ALARMS, fence=SIM_FENCE)
    cooled.state(MowerActivity.MOWING, position=at_local(5, 4))
    cooled.state(MowerActivity.MOWING, position=far)
    assert cooled.state(MowerActivity.MOWING, position=far) == ["geofence_exit"]
    cooled.state(MowerActivity.MOWING, position=at_local(5, 4))
    cooled.state(MowerActivity.MOWING, position=far)
    assert cooled.state(MowerActivity.MOWING, position=far) == []


def test_notifications_theft_geofence_ignores_rtk_none_and_manual_driving() -> None:
    far = at_local(32, 4)
    run = Run(ALARMS, fence=SIM_FENCE)
    run.state(MowerActivity.IDLE, position=at_local(5, 4))
    for _ in range(3):
        assert run.state(MowerActivity.IDLE, position=far, rtk_fix=RtkFixStatus.NONE) == []
    run.command(drive=True)
    for _ in range(3):
        assert run.state(MowerActivity.IDLE, position=far, after=60) == []
    assert run.watch is not None and run.watch.outside_count == 0
    assert run.state(MowerActivity.IDLE, position=far, after=600) == []  # window over
    assert run.state(MowerActivity.IDLE, position=far) == ["geofence_exit"]
    unmapped = Run(ALARMS)
    unmapped.state(MowerActivity.IDLE, position=far)
    assert unmapped.state(MowerActivity.IDLE, position=far) == []


async def test_notifications_theft_simulated_job_raises_no_alarm() -> None:
    clock = SimClock(T0)
    sim = SimulatedMower(config=SimulationConfig(time_scale=1.0), clock=clock)
    await sim.connect()
    watch, _ = observe_state(None, sim.snapshot(), clock(), ALARMS, SIM_FENCE)
    watch = observe_command(watch, clock(), drive=False)  # the scheduler's start command
    await sim.execute(StartMow(area_ids=["front", "back"]))
    events: list[NotificationEvent] = []
    activities: set[MowerActivity] = set()
    states = 0
    for _ in range(20_000):
        clock.advance(2)
        state = sim.tick(2)
        watch, derived = observe_state(watch, state, clock(), ALARMS, SIM_FENCE)
        events.extend(derived)
        activities.add(state.activity)
        states += 1
        if state.activity in (MowerActivity.DOCKED, MowerActivity.CHARGING) and states > 10:
            break
    assert MowerActivity.MOWING in activities and states > 100
    assert state.activity in (MowerActivity.DOCKED, MowerActivity.CHARGING)
    assert [e.kind for e in events if e.kind.value in THEFT] == []
    assert watch.outside_count == 0
    # the same mower lifted afterwards raises the lift alarm
    sim.lift()
    clock.advance(2)
    _, derived = observe_state(watch, sim.snapshot(), clock(), ALARMS, SIM_FENCE)
    assert [e.kind for e in derived] == [NotificationKind.MOWER_LIFTED]


def test_notifications_theft_fence_from_map_and_json() -> None:
    assert SIM_FENCE is not None
    assert Fence.from_json(SIM_FENCE.to_json()) == SIM_FENCE
    placeholder = SIM_MAP.model_copy(update={"reference": GeoReference(origin_lat=0, origin_lon=0)})
    assert Fence.from_map(placeholder) is None
    assert Fence.from_map(SIM_MAP.model_copy(update={"areas": []})) is None
    bare = Fence(origin_lat=48.0, origin_lon=11.0, rotation_deg=0, areas=())
    assert fence_distance(bare, (1, 1)) == float("inf")
    dot = Fence(48.0, 11.0, 0, areas=(((0, 0), (1, 0), (0, 1)),), paths=(((5.0, 5.0),),))
    assert fence_distance(dot, (5, 8)) == 3
    assert segment_distance((3, 4), (0, 0), (0, 0)) == 5


def test_notifications_blade_maintenance_event_and_cooldown() -> None:
    crossing = BladeHoursThreshold(
        mower_id=MOWER,
        account_id=ACCOUNT,
        blade_hours_since_change=50.4,
        threshold_hours=50,
        crossed_at=T0,
    )
    watch, events = observe_blade_hours(None, crossing, T0, CONFIG)
    assert watch is None and [e.kind for e in events] == [NotificationKind.BLADE_MAINTENANCE]
    event = events[0]
    assert event.params == {"hours": 50, "threshold": 50}
    assert event.severity is NotificationSeverity.INFO
    assert event.dedupe_key == f"blade_maintenance:{MOWER}:{T0.isoformat()}"
    run = Run()
    run.state(MowerActivity.DOCKED)
    assert run.watch is not None
    watch, events = observe_blade_hours(run.watch, crossing, T0, CONFIG)
    assert watch is not None and len(events) == 1
    _, again = observe_blade_hours(watch, crossing, T0 + seconds(10), CONFIG)
    assert again == []  # normal cooldown


def test_notifications_blade_maintenance_uses_telemetrys_effective_threshold() -> None:
    # Telemetry sends the counter it shows (the mower's own) and the one effective threshold
    # (here the mower's own warning); the reminder names exactly those, no second threshold.
    crossing = BladeHoursThreshold(
        mower_id=MOWER,
        account_id=ACCOUNT,
        blade_hours_since_change=100.2,
        threshold_hours=100,
        crossed_at=T0,
    )
    _, events = observe_blade_hours(None, crossing, T0, CONFIG)
    assert events[0].params == {"hours": 100, "threshold": 100}


def _firmware(**changes: object) -> FirmwareInfo:
    values: dict[str, object] = {"installed": "1.30.29.26", "update_state": "idle"}
    values.update(changes)
    return FirmwareInfo.model_validate(values)


def test_notifications_rules_firmware_offer_once_per_version() -> None:
    run = Run(RuleConfig(cooldown_seconds=0))
    offer = _firmware(update_state="available", available="1.30.31.19")
    assert run.state(MowerActivity.DOCKED, firmware_info=offer) == []  # first sight: quiet
    assert run.watch is not None and run.watch.firmware_offered == "1.30.31.19"
    assert run.state(MowerActivity.DOCKED, firmware_info=offer) == []  # the same offer again
    newer = _firmware(update_state="available", available="1.30.32.1")
    assert run.state(MowerActivity.DOCKED, firmware_info=newer) == ["firmware_update_available"]
    event = run.events[-1]
    assert event.params == {"version": "1.30.32.1", "installed": "1.30.29.26"}
    assert event.severity is NotificationSeverity.INFO
    assert event.dedupe_key == f"firmware_update_available:{MOWER}:1.30.32.1"
    assert event.link == f"/mowers/{MOWER}/settings"
    assert run.state(MowerActivity.DOCKED, firmware_info=newer) == []
    assert run.state(MowerActivity.DOCKED, firmware_info=_firmware()) == []  # withdrawn: quiet
    # The same version offered again later notifies again (it was gone meanwhile)... only when
    # the watch forgot it: the offer stays remembered until an update finishes.
    assert run.state(MowerActivity.DOCKED, firmware_info=newer) == []


def test_notifications_rules_firmware_update_finished_and_failed() -> None:
    run = Run(RuleConfig(cooldown_seconds=0))
    run.state(MowerActivity.DOCKED, firmware_info=_firmware())
    offer = _firmware(update_state="available", available="1.30.31.19")
    assert run.state(MowerActivity.CHARGING, firmware_info=offer) == ["firmware_update_available"]
    installing = _firmware(update_state="installing", available="1.30.31.19", progress=40)
    assert run.state(MowerActivity.UPDATING, firmware_info=installing) == []
    assert run.state(MowerActivity.UPDATING, firmware_info=installing) == []
    done = _firmware(installed="1.30.31.19", update_state="idle")
    assert run.state(MowerActivity.DOCKED, firmware_info=done) == ["firmware_update_finished"]
    assert run.events[-1].params == {"version": "1.30.31.19"}
    assert run.watch is not None and run.watch.firmware_offered is None
    assert run.state(MowerActivity.DOCKED, firmware_info=done) == []
    # A failed update.
    assert run.state(
        MowerActivity.CHARGING,
        firmware_info=_firmware(
            installed="1.30.31.19", update_state="available", available="1.30.33.0"
        ),
    ) == ["firmware_update_available"]
    run.state(
        MowerActivity.UPDATING,
        firmware_info=_firmware(
            installed="1.30.31.19", update_state="installing", available="1.30.33.0"
        ),
    )
    failed = _firmware(
        installed="1.30.31.19", update_state="failed", failure_reason="dl fw: checksum"
    )
    assert run.state(MowerActivity.DOCKED, firmware_info=failed) == ["firmware_update_failed"]
    event = run.events[-1]
    assert event.severity is NotificationSeverity.WARNING
    assert event.params == {"version": "1.30.33.0", "reason": "dl fw: checksum"}
    assert run.state(MowerActivity.DOCKED, firmware_info=failed) == []
    # An update nobody announced (started in the vendor app) still reports its end.
    run.state(
        MowerActivity.UPDATING,
        firmware_info=_firmware(installed="1.30.31.19", update_state="installing"),
    )
    assert run.state(MowerActivity.DOCKED, firmware_info=_firmware(installed="1.30.34.0")) == [
        "firmware_update_finished"
    ]
    # No firmware information at all: nothing.
    assert run.state(MowerActivity.DOCKED) == []


def test_notifications_rules_firmware_watch_round_trips() -> None:
    run = Run()
    run.state(
        MowerActivity.DOCKED, firmware_info=_firmware(update_state="available", available="2")
    )
    assert run.watch is not None
    data = run.watch.to_json()
    assert data["firmware_state"] == "available" and data["firmware_offered"] == "2"
    restored = MowerWatch.from_json(MOWER, ACCOUNT, data)
    assert restored == run.watch


def test_rules_vision_resume_proposal_has_its_own_body() -> None:
    from mowdeck_contracts.v1 import PrincipalKind, PrincipalRef, VisionProposalCreated, VisionTask

    from notifications.domain.rules import vision_proposal_event
    from notifications.messages import load_catalog

    mower_id = uuid4()
    payload = VisionProposalCreated(
        proposal_id=uuid4(),
        mower_id=mower_id,
        account_id=uuid4(),
        task=VisionTask.RECOVER,
        round=1,
        requested_by=PrincipalRef(subject=uuid4(), kind=PrincipalKind.USER),
        situation="stuck_on_obstacle",
        confidence=0.8,
        steps=1,
        action="resume",
        expires_at=T0 + timedelta(minutes=5),
        link=f"/mowers/{mower_id}?assist=1",
        created_at=T0,
    )
    event = vision_proposal_event(payload, 5)
    assert event.body_key == "vision_proposal.body_resume"
    catalog = load_catalog()
    for language in ("de", "en"):
        assert "vision_proposal.body_resume" in catalog.keys(language)
        assert "vision_auto_proposal.body_resume" in catalog.keys(language)
    body = catalog.render("en", event.body_key, event.params)
    assert body.startswith("Continue the paused mowing job")
