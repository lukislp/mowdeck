"""Husqvarna data to MowDeck state, capabilities, map and device list."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from aioautomower.model import MowerActivities, MowerAttributes, MowerStates
from husqvarna_support import (
    HUGO,
    LISA_CHARGING,
    LISA_LIFTED,
    attributes,
    mowers_payload,
    parse,
)
from mowdeck_contracts.v1 import (
    ConnectionKind,
    DeviceType,
    MowerActivity,
    MowerErrorCategory,
    MowerState,
    PositionSource,
)
from mowdeck_control_husqvarna import mapping

MOWER_ID, ACCOUNT_ID = uuid4(), uuid4()


def _with(
    state: MowerStates,
    activity: MowerActivities,
    *,
    connected: bool = True,
    error_code: int = 0,
) -> MowerAttributes:
    payload = mowers_payload()
    item = next(m for m in payload["data"] if m["id"] == HUGO)
    item["attributes"]["mower"].update(
        state=state.value.upper(), activity=activity.value.upper(), errorCode=error_code
    )
    item["attributes"]["metadata"]["connected"] = connected
    return parse(payload)[HUGO]


def _state(source: MowerAttributes) -> MowerState:
    return mapping.state_of(source, mower_id=MOWER_ID, account_id=ACCOUNT_ID)


def test_discovery_lists_every_mower_with_unique_names() -> None:
    devices = mapping.discovered_devices(parse(mowers_payload()))
    assert [(d.device_name, d.iot_id, d.model, d.online) for d in devices] == [
        ("Hugo", HUGO, "Automower 430X NERA", True),
        ("Lisa (182200001)", LISA_CHARGING, "Automower 315 Mark II", True),
        ("Lisa (222300002)", LISA_LIFTED, "Automower 405X", True),
    ]
    assert all(d.is_mower and d.device_type is DeviceType.MOWER for d in devices)


# Every Husqvarna state while the mower reports "mowing", in the order of the module rules.
STATE_TABLE = [
    (MowerStates.IN_OPERATION, MowerActivity.MOWING),
    (MowerStates.RESTRICTED, MowerActivity.MOWING),
    (MowerStates.PAUSED, MowerActivity.PAUSED),
    (MowerStates.ERROR, MowerActivity.ERROR),
    (MowerStates.FATAL_ERROR, MowerActivity.ERROR),
    (MowerStates.ERROR_AT_POWER_UP, MowerActivity.ERROR),
    (MowerStates.STOPPED, MowerActivity.IDLE),
    (MowerStates.OFF, MowerActivity.MOWING),
    (MowerStates.WAIT_UPDATING, MowerActivity.UPDATING),
    (MowerStates.WAIT_POWER_UP, MowerActivity.MOWING),
    (MowerStates.UNKNOWN, MowerActivity.MOWING),
    (MowerStates.NOT_APPLICABLE, MowerActivity.MOWING),
]

# Every Husqvarna activity while the mower is in operation.
ACTIVITY_TABLE = [
    (MowerActivities.MOWING, MowerActivity.MOWING),
    (MowerActivities.LEAVING, MowerActivity.MOWING),
    (MowerActivities.GOING_HOME, MowerActivity.RETURNING),
    (MowerActivities.CHARGING, MowerActivity.CHARGING),
    (MowerActivities.PARKED_IN_CS, MowerActivity.DOCKED),
    (MowerActivities.STOPPED_IN_GARDEN, MowerActivity.IDLE),
    (MowerActivities.UNKNOWN, MowerActivity.UNKNOWN),
    (MowerActivities.NOT_APPLICABLE, MowerActivity.UNKNOWN),
]


def test_tables_cover_every_vendor_value() -> None:
    assert {s for s, _ in STATE_TABLE} == set(MowerStates)
    assert {a for a, _ in ACTIVITY_TABLE} == set(MowerActivities)


@pytest.mark.parametrize(("state", "expected"), STATE_TABLE)
def test_states_map_onto_activities(state: MowerStates, expected: MowerActivity) -> None:
    assert mapping.activity_of(_with(state, MowerActivities.MOWING)) is expected


@pytest.mark.parametrize(("activity", "expected"), ACTIVITY_TABLE)
def test_activities_map_onto_activities(activity: MowerActivities, expected: MowerActivity) -> None:
    assert mapping.activity_of(_with(MowerStates.IN_OPERATION, activity)) is expected


@pytest.mark.parametrize(
    ("state", "activity", "connected", "code", "expected"),
    [
        (MowerStates.IN_OPERATION, MowerActivities.MOWING, False, 0, MowerActivity.OFFLINE),
        (MowerStates.STOPPED, MowerActivities.NOT_APPLICABLE, True, 9, MowerActivity.ERROR),
        (MowerStates.OFF, MowerActivities.PARKED_IN_CS, True, 0, MowerActivity.DOCKED),
        (MowerStates.OFF, MowerActivities.NOT_APPLICABLE, True, 0, MowerActivity.IDLE),
        (MowerStates.PAUSED, MowerActivities.GOING_HOME, True, 0, MowerActivity.PAUSED),
        (MowerStates.RESTRICTED, MowerActivities.PARKED_IN_CS, True, 0, MowerActivity.DOCKED),
        # a warning code outside an error state keeps the activity
        (MowerStates.IN_OPERATION, MowerActivities.MOWING, True, 27, MowerActivity.MOWING),
    ],
)
def test_special_cases(
    state: MowerStates,
    activity: MowerActivities,
    connected: bool,
    code: int,
    expected: MowerActivity,
) -> None:
    source = _with(state, activity, connected=connected, error_code=code)
    assert mapping.activity_of(source) is expected


def test_a_mowing_mower_reports_battery_position_and_derived_blades() -> None:
    state = _state(attributes(HUGO))
    assert state.activity is MowerActivity.MOWING
    assert state.connection is ConnectionKind.CLOUD
    assert state.battery_percent == 87 and not state.charging
    assert state.position is not None
    assert (state.position.lat, state.position.lon) == (57.7086409, 14.1678988)
    assert state.position_source is PositionSource.UNKNOWN
    assert state.raw["blade_on"] is True and state.raw["blade_on_source"] == "activity"
    assert state.raw["work_area_name"] == "Front lawn"
    assert state.raw["work_areas"] == {"0": "my_lawn", "123456": "Front lawn"}
    assert state.raw["cutting_height_level"] == 4
    assert state.raw["headlight_mode"] == "evening_only"
    assert (state.raw["blade_used_s"], state.raw["bat_cycles"]) == (123456, 421)
    assert (state.raw["work_time_s"], state.raw["mileage_m"]) == (4334400, 1780272)
    assert state.error_code is None and "lifted" not in state.raw
    assert state.observed_at == datetime.fromtimestamp(1793606400, tz=UTC)


def test_blades_are_off_unless_mowing_in_operation() -> None:
    for state, activity in [
        (MowerStates.PAUSED, MowerActivities.MOWING),
        (MowerStates.IN_OPERATION, MowerActivities.LEAVING),
        (MowerStates.IN_OPERATION, MowerActivities.GOING_HOME),
    ]:
        assert _state(_with(state, activity)).raw["blade_on"] is False


def test_a_charging_mower_without_gps() -> None:
    state = _state(attributes(LISA_CHARGING))
    assert state.activity is MowerActivity.CHARGING and state.charging
    assert state.position is None
    assert state.raw["remaining_charging_s"] == 1800
    assert state.raw["restricted_reason"] == "week_schedule"
    assert "next_start" in state.raw
    assert "blade_used_s" not in state.raw


@pytest.mark.parametrize("code", sorted(mapping.LIFT_CODES))
def test_lift_codes_raise_the_theft_alarm_flag(code: int) -> None:
    state = _state(attributes(LISA_LIFTED, errorCode=code))
    assert state.activity is MowerActivity.ERROR
    assert state.raw["lifted"] is True
    assert state.raw["lift_code"] == str(code)
    assert state.error_code == str(code)
    assert state.error_category is MowerErrorCategory.LIFT_TILT
    assert state.error_message == mapping.error_text(code)
    assert not state.drive_recoverable


def test_the_recorded_lifted_mower() -> None:
    state = _state(attributes(LISA_LIFTED))
    assert (state.error_code, state.error_message) == ("15", "Lifted")
    assert state.raw["error_key"] == "lifted"


@pytest.mark.parametrize(
    ("code", "category"),
    [
        (10, MowerErrorCategory.LIFT_TILT),
        (9, MowerErrorCategory.TRAPPED),
        (1, MowerErrorCategory.OUTSIDE),
        (78, MowerErrorCategory.WHEEL_SLIP),
        (12, MowerErrorCategory.BATTERY),
        (24, MowerErrorCategory.BLADE_MOTOR),
        (93, MowerErrorCategory.POSITIONING),
        (148, MowerErrorCategory.COLLISION),
        (701, MowerErrorCategory.OTHER),
    ],
)
def test_other_errors_are_categorised_without_the_lift_flag(
    code: int, category: MowerErrorCategory
) -> None:
    state = _state(attributes(LISA_LIFTED, errorCode=code))
    assert state.error_category is category
    assert "lifted" not in state.raw


def test_a_code_outside_an_error_is_kept_aside() -> None:
    state = _state(_with(MowerStates.IN_OPERATION, MowerActivities.MOWING, error_code=14))
    assert state.error_code is None and "lifted" not in state.raw
    assert state.raw["last_error_code"] == "14"


def test_observed_at_follows_later_events() -> None:
    source = attributes(HUGO)
    later = source.metadata.status_dateteime + timedelta(minutes=3)
    state = mapping.state_of(source, mower_id=MOWER_ID, account_id=ACCOUNT_ID, observed_at=later)
    assert state.observed_at == later


def test_capabilities_are_honest() -> None:
    caps = mapping.capabilities_of(attributes(HUGO))
    assert caps.model == "Automower 430X NERA"
    assert caps.connection_kinds == frozenset({ConnectionKind.CLOUD})
    assert not caps.native_patterns and not caps.manual_drive and not caps.custom_paths
    assert not caps.light and not caps.rtk_sources and caps.blade_height_range_mm is None
    assert not caps.settings and not caps.maintenance and not caps.firmware_update
    assert mapping.capabilities_of(None).model is None


def test_the_map_has_a_reference_and_no_geometry() -> None:
    position = _state(attributes(HUGO)).position
    found = mapping.map_of(position)
    assert (found.reference.origin_lat, found.reference.origin_lon) == (57.7086409, 14.1678988)
    assert found.areas == [] and found.no_go_zones == [] and found.dock is None
    assert mapping.map_of(None).reference.origin_lat == 0.0
