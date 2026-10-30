"""Product items to MowDeck: the snapshot reader, the state table, errors, capabilities."""

from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import (
    ConnectionKind,
    MowerActivity,
    MowerErrorCategory,
    MowerState,
    PositionSource,
    StartPlanKind,
)
from mowdeck_control_worx import mapping
from mowdeck_control_worx.snapshot import WorxMower
from worx_support import PLAIN, SILENT, VISION, WIRE, all_items, item, mower

MOWER_ID, ACCOUNT_ID = uuid4(), uuid4()


def _state(m: WorxMower, previous: MowerState | None = None) -> MowerState:
    return mapping.state_of(m, mower_id=MOWER_ID, account_id=ACCOUNT_ID, previous=previous)


def test_the_reader_takes_what_the_cloud_lists() -> None:
    wire = mower(WIRE)
    assert (wire.serial_number, wire.name, wire.model) == (
        WIRE,
        "Rasenwerk",
        "Landroid L 2000 WR155E",
    )
    assert wire.online and wire.protocol == 0
    assert (wire.status_code, wire.error_code, wire.battery_percent) == (7, 0, 87)
    assert wire.gps == (52.52045, 13.40495, "4g")
    assert wire.has_cutting_height and wire.cutting_height_mm == 45
    assert wire.has_edge_cut and wire.rain_delay_min == 60
    assert wire.zone_starts == [12, 35, 0, 0] and wire.zones == [0, 1]
    assert wire.current_zone == 1  # lz 3 is the fourth slot of the rotation
    assert wire.firmware == "3.3" and wire.wifi_dbm == -58
    silent = WorxMower.from_item(item(SILENT))
    assert silent.name == "No Name" and not silent.online
    assert silent.status_code is None and silent.battery_percent is None
    assert silent.zones == [] and not silent.has_edge_cut and silent.rain_delay_min is None
    with pytest.raises(ValueError, match="serial"):
        WorxMower.from_item({"name": "x"})


def test_the_reader_skips_odd_values() -> None:
    odd = mower(WIRE, bt={"p": "x", "c": "1"}, rsi=7, ls="7", fw="  ")
    assert odd.battery_percent is None and odd.charging
    assert odd.wifi_dbm is None and odd.status_code == 7 and odd.firmware is None
    assert mower(WIRE, modules={"4G": {"gps": {"coo": [0, 0]}}}).gps is None
    assert mower(WIRE, modules={"4G": {"gps": {"coo": [95.0, 1.0]}}}).gps is None
    assert mower(WIRE, bt={"p": 140}).battery_percent == 100


@pytest.mark.parametrize(
    ("status", "activity"),
    [
        (0, MowerActivity.IDLE),
        (1, MowerActivity.DOCKED),
        (2, MowerActivity.MOWING),
        (3, MowerActivity.MOWING),
        (5, MowerActivity.RETURNING),
        (6, MowerActivity.RETURNING),
        (7, MowerActivity.MOWING),
        (8, MowerActivity.MOWING),
        (9, MowerActivity.ERROR),
        (10, MowerActivity.ERROR),
        (11, MowerActivity.UNKNOWN),
        (12, MowerActivity.UNKNOWN),
        (13, MowerActivity.MOWING),
        (30, MowerActivity.RETURNING),
        (31, MowerActivity.MOWING),
        (32, MowerActivity.MOWING),
        (33, MowerActivity.MOWING),
        (34, MowerActivity.PAUSED),
        (103, MowerActivity.MOWING),
        (104, MowerActivity.RETURNING),
        (110, MowerActivity.MOWING),
        (111, MowerActivity.MOWING),
        (-1, MowerActivity.UNKNOWN),
        (99, MowerActivity.UNKNOWN),
    ],
)
def test_statuses_map_onto_activities(status: int, activity: MowerActivity) -> None:
    assert mapping.activity_of(mower(WIRE, ls=status, le=0)) is activity


def test_follow_wire_depends_on_what_came_before() -> None:
    follow = mower(WIRE, ls=4)
    assert mapping.activity_of(follow) is MowerActivity.MOWING
    assert mapping.activity_of(follow, MowerActivity.RETURNING) is MowerActivity.RETURNING
    homing = _state(mower(WIRE, ls=30))
    assert _state(follow, previous=homing).activity is MowerActivity.RETURNING


def test_home_while_charging_offline_and_unknown() -> None:
    assert mapping.activity_of(mower(PLAIN)) is MowerActivity.CHARGING
    assert mapping.activity_of(mower(PLAIN, bt={"p": 100, "c": 0})) is MowerActivity.DOCKED
    assert mapping.activity_of(mower(WIRE, online=False)) is MowerActivity.OFFLINE
    assert mapping.activity_of(WorxMower.from_item(item(SILENT))) is MowerActivity.OFFLINE
    assert mapping.activity_of(mower(WIRE, ls=None)) is MowerActivity.UNKNOWN


def test_the_full_state_of_a_mowing_mower() -> None:
    state = _state(mower(WIRE))
    assert state.activity is MowerActivity.MOWING and state.connection is ConnectionKind.CLOUD
    assert state.battery_percent == 87 and not state.charging
    assert state.position is not None and state.position.lat == 52.52045
    assert state.position_source is PositionSource.GNSS
    assert state.blade_height_mm == 45 and state.firmware == "3.3"
    assert state.signal is not None and state.signal.wifi_dbm == -58
    assert state.rain_detected is False and state.settings == {"rain_delay": 60}
    assert state.error_code is None and state.error_category is None
    raw = state.raw
    assert (
        raw["vendor"] == "worx" and raw["worx_status"] == 7 and raw["worx_status_name"] == "mowing"
    )
    assert raw["blade_on"] is True and raw["blade_on_source"] == "status"
    assert raw["zone"] == 2 and raw["locked"] is False
    assert raw["blade_used_s"] == 21840 * 60 and raw["work_time_s"] == 33120 * 60
    assert raw["mileage_m"] == 523456 and raw["bat_cycles"] == 312
    assert "lifted" not in raw


def test_blades_turn_only_in_the_cutting_statuses() -> None:
    for status, blades in ((7, True), (32, True), (110, True), (31, False), (3, False), (1, False)):
        assert _state(mower(WIRE, ls=status)).raw["blade_on"] is blades, status


def test_a_lifted_mower_raises_the_theft_alarm() -> None:
    state = _state(mower(VISION))
    assert state.activity is MowerActivity.ERROR
    assert (state.error_code, state.error_message) == ("2", "Lifted")
    assert state.error_category is MowerErrorCategory.LIFT_TILT
    assert state.raw["lifted"] is True and state.raw["lift_code"] == "2"
    assert state.raw["blade_on"] is False
    upside = _state(mower(WIRE, le=11))
    assert upside.raw["lift_code"] == "11" and upside.error_category is MowerErrorCategory.LIFT_TILT
    slope = _state(mower(WIRE, le=104))
    assert slope.error_category is MowerErrorCategory.OTHER and "lifted" not in slope.raw


def test_vision_positions_have_no_quality() -> None:
    state = _state(mower(VISION, le=0, ls=7))
    assert state.position is not None and state.position.lat == 48.13743
    assert state.position_source is PositionSource.UNKNOWN
    assert state.blade_height_mm == 50 and state.firmware == "4.1.2"


def test_the_rain_delay_is_no_error() -> None:
    state = _state(mower(PLAIN, le=5))
    assert state.activity is MowerActivity.CHARGING and state.error_code is None
    assert state.raw["last_error_code"] == "5" and state.raw["rain_delay_remaining_min"] == 142
    assert state.rain_detected is True and state.settings == {"rain_delay": 180}


def test_statuses_trapped_and_blade_blocked_without_an_error_code() -> None:
    trapped = _state(mower(WIRE, ls=9, le=0))
    assert trapped.activity is MowerActivity.ERROR and trapped.error_code is None
    assert trapped.error_message == "Trapped"
    assert trapped.error_category is MowerErrorCategory.TRAPPED
    blocked = _state(mower(WIRE, ls=10, le=0))
    assert blocked.error_category is MowerErrorCategory.BLADE_MOTOR
    unknown = _state(mower(WIRE, le=987))
    assert unknown.error_code == "987" and unknown.error_message == "Unknown error 987"


def test_the_battery_stands_in_while_a_report_has_none() -> None:
    before = _state(mower(WIRE))
    after = _state(mower(WIRE, bt=None), previous=before)
    assert after.battery_percent == 87
    assert _state(mower(WIRE, bt=None)).battery_percent == 0


def test_capabilities_declare_only_what_the_mower_offers() -> None:
    wire = mapping.capabilities_of(mower(WIRE))
    assert wire.model == "Landroid L 2000 WR155E"
    assert wire.connection_kinds == frozenset({ConnectionKind.CLOUD})
    assert wire.blade_height_range_mm == (30, 60)
    assert [s.key for s in wire.settings] == ["rain_delay"]
    assert [(p.ref, p.kind, p.name) for p in wire.start_plans] == [
        ("now", StartPlanKind.NOW, None),
        ("zone:1", StartPlanKind.ZONE, "Zone 1"),
        ("zone:2", StartPlanKind.ZONE, "Zone 2"),
        ("edge", StartPlanKind.EDGE, None),
    ]
    assert not wire.native_patterns and not wire.manual_drive and not wire.custom_paths
    assert not wire.light and not wire.rtk_sources and not wire.maintenance
    assert not wire.firmware_update and wire.drive_speed_setting is None
    no_zones = mapping.capabilities_of(mower(WIRE), offer_zones=False)
    assert [p.ref for p in no_zones.start_plans] == ["now", "edge"]
    plain = mapping.capabilities_of(mower(PLAIN))
    assert plain.blade_height_range_mm is None
    assert [p.ref for p in plain.start_plans] == ["now"]
    assert [s.key for s in plain.settings] == ["rain_delay"]
    silent = mapping.capabilities_of(WorxMower.from_item(item(SILENT)))
    assert silent.settings == [] and [p.ref for p in silent.start_plans] == ["now"]
    assert mapping.capabilities_of(None).start_plans == []
    vision = mapping.capabilities_of(mower(VISION))
    assert [p.ref for p in vision.start_plans] == ["now", "edge"]  # one-time schedule (once)


def test_a_single_zone_is_no_choice() -> None:
    data = item(WIRE)
    data["last_status"]["payload"]["cfg"]["mz"] = [12, 0, 0, 0]
    assert WorxMower.from_item(data).zones == []


def test_the_map_has_no_geometry() -> None:
    empty = mapping.map_of(None)
    assert empty.areas == [] and empty.reference.origin_lat == 0
    state = _state(mower(WIRE))
    assert mapping.map_of(state.position).reference.origin_lat == 52.52045


def test_discovered_devices_have_unique_names() -> None:
    mowers = {m.serial_number: m for m in (WorxMower.from_item(i) for i in all_items())}
    devices = mapping.discovered_devices(mowers)
    assert [d.device_name for d in devices] == ["Gartenzwerg", "No Name", "Rasenwerk", "Vision"]
    by_name = {d.device_name: d for d in devices}
    assert by_name["Rasenwerk"].iot_id == WIRE and by_name["Rasenwerk"].online
    assert by_name["No Name"].model == "Landroid M 700 WG794E" and not by_name["No Name"].online
    twin = item(PLAIN)
    twin["serial_number"], twin["name"] = "S2", "Rasenwerk"
    names = mapping.device_names({WIRE: mower(WIRE), "S2": WorxMower.from_item(twin)})
    assert names == {WIRE: f"Rasenwerk ({WIRE})", "S2": "Rasenwerk (S2)"}
