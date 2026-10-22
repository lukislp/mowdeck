"""State, capability, map and command mapping on real PyMammotion data classes."""

import math
from typing import Any

import pytest
from mammotion_support import ACCOUNT_ID, MOWER_ID, NOW
from mowdeck_contracts.v1 import (
    ConnectionKind,
    MowerActivity,
    MowerCapabilities,
    MowerState,
    MowPattern,
    PositionSource,
    RtkFixStatus,
    RtkSource,
    StartMow,
    TurnStyle,
)
from mowdeck_control import UnsupportedPattern
from mowdeck_control.geo import from_wgs84, to_wgs84
from mowdeck_control_mammotion import mapping
from mowdeck_control_mammotion.fake import (
    BACK_HASH,
    DOCK_ROTATION_DEG,
    DOCK_X,
    DOCK_Y,
    FRONT_HASH,
    NOGO_HASH,
    OBSTACLE_HASH,
    PATH_HASH,
    RTK_LAT_DEG,
    RTK_LON_DEG,
    RTK_YAW_DEG,
    dock_buffer,
    frames,
    sample_device,
)
from mowdeck_control_mammotion.port import (
    CuttingMode,
    MowerDevice,
    PathAngleSetting,
    PathType,
    WorkMode,
)


def _state(
    device: MowerDevice,
    *,
    has_report: bool = True,
    firmware: str = "1.30.29.26",
    link: str | None = None,
    model: str = "",
) -> MowerState:
    return mapping.state_of(
        device,
        mower_id=MOWER_ID,
        account_id=ACCOUNT_ID,
        connection=ConnectionKind.CLOUD,
        device_name="Luba-VP1",
        firmware=firmware,
        observed_at=NOW,
        has_report=has_report,
        link=link,
        model=model,
    )


# --- state ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "charge_state", "expected"),
    [
        (WorkMode.MODE_WORKING, 0, MowerActivity.MOWING),
        (WorkMode.MODE_PAUSE, 0, MowerActivity.PAUSED),
        (WorkMode.MODE_CHARGING_PAUSE, 1, MowerActivity.PAUSED),
        (WorkMode.MODE_RETURNING, 0, MowerActivity.RETURNING),
        (WorkMode.MODE_CHARGING, 1, MowerActivity.CHARGING),
        (WorkMode.MODE_READY, 1, MowerActivity.DOCKED),
        (WorkMode.MODE_READY, 0, MowerActivity.IDLE),
        (WorkMode.MODE_MANUAL_MOWING, 0, MowerActivity.IDLE),
        (WorkMode.MODE_SYSTEM_ERROR, 0, MowerActivity.ERROR),
        (WorkMode.MODE_LOCK, 0, MowerActivity.ERROR),
        (WorkMode.MODE_LOCATION_ERROR, 0, MowerActivity.ERROR),
        (WorkMode.MODE_SLEEPING, 1, MowerActivity.OFFLINE),
        (WorkMode.MODE_POWER_OFF, 0, MowerActivity.OFFLINE),
        (WorkMode.MODE_NOT_ACTIVE, 0, MowerActivity.UNKNOWN),
    ],
)
def test_mammotion_activity_from_work_mode(
    mode: WorkMode, charge_state: int, expected: MowerActivity
) -> None:
    device = sample_device()
    device.report_data.dev.sys_status = int(mode)
    device.report_data.dev.charge_state = charge_state
    assert mapping.activity_of(device) is expected


def test_mammotion_unknown_mode_offline_flag_and_missing_report() -> None:
    device = sample_device()
    device.report_data.dev.sys_status = 999
    assert mapping.activity_of(device) is MowerActivity.UNKNOWN
    device.report_data.dev.sys_status = int(WorkMode.MODE_WORKING)
    assert mapping.activity_of(device, has_report=False) is MowerActivity.UNKNOWN
    device.online = False
    assert mapping.activity_of(device) is MowerActivity.OFFLINE


def test_mammotion_state_snapshot_of_a_docked_mower() -> None:
    state = _state(sample_device(), link="cloud")
    assert state.activity is MowerActivity.DOCKED
    assert state.battery_percent == 87
    assert state.charging is True
    assert state.rtk_fix is RtkFixStatus.FIXED
    assert state.rtk_source is RtkSource.NETWORK
    assert state.connection is ConnectionKind.CLOUD
    assert state.blade_height_mm == 55
    assert state.speed_mps == pytest.approx(0.4)
    assert state.firmware == "1.30.29.26"
    assert state.error_code is None
    # docked with a known dock: the dock pose, not the raw position
    assert state.position_source is PositionSource.DOCK
    assert state.position is not None
    assert state.position.heading == pytest.approx(DOCK_ROTATION_DEG % 360)  # compass, as reported
    assert state.raw["rtk_switch"] == "RTK_USED_NRTK"
    assert state.raw["work_mode"] == "MODE_READY"
    assert state.raw["link"] == "cloud"
    assert "imei" not in str(state.raw).lower()


def test_mammotion_state_without_position_full_battery_and_odd_values() -> None:
    device = sample_device()
    device.location.device.latitude = 0.0
    device.location.device.longitude = 0.0
    device.report_data.dev.battery_val = 100
    device.report_data.work.knife_height = 0
    device.mower_state.travel_speed = 0.0
    device.location.dock.latitude = device.location.dock.longitude = 0.0  # dock unknown
    state = _state(device, firmware="")
    assert state.position is None
    assert state.position_source is PositionSource.UNKNOWN
    assert state.firmware is None
    assert state.charging is False  # full battery on the dock
    assert state.blade_height_mm is None and state.speed_mps is None
    device.location.device.latitude = 400.0  # garbage from an uninitialised converter
    device.location.device.longitude = 1.0
    assert mapping.position_of(device) is None


def _raw_position_lat() -> float:
    return RTK_LAT_DEG + 0.00001


def test_mammotion_docked_position_snaps_to_the_dock_in_wgs84() -> None:
    device = sample_device()
    device.report_data.rtk.status = 1  # RTKStatus.SINGLE: no fix in the dock
    state = _state(device)
    assert state.activity is MowerActivity.DOCKED
    assert state.rtk_fix is RtkFixStatus.NONE
    assert state.position_source is PositionSource.DOCK
    assert state.position is not None
    mower_map = mapping.map_of(device)
    lat, lon = to_wgs84(mower_map, DOCK_X, DOCK_Y)  # the dock where the map draws it
    assert state.position.lat == pytest.approx(lat, abs=1e-7)
    assert state.position.lon == pytest.approx(lon, abs=1e-7)
    x, y = from_wgs84(mower_map, state.position.lat, state.position.lon)
    assert math.hypot(x - DOCK_X, y - DOCK_Y) < 0.01
    # a scattering raw position does not move the reported one
    device.location.device.latitude += 0.00004
    assert _state(device).position == state.position


@pytest.mark.parametrize(
    ("mode", "charge_state"),
    [
        (WorkMode.MODE_CHARGING, 1),  # charging
        (WorkMode.MODE_READY, 1),  # docked
        (WorkMode.MODE_CHARGING_PAUSE, 1),  # job paused on the charger
        (WorkMode.MODE_PAUSE, 1),
    ],
)
def test_mammotion_position_snaps_in_the_dock(mode: WorkMode, charge_state: int) -> None:
    device = sample_device()
    device.report_data.dev.sys_status = int(mode)
    device.report_data.dev.charge_state = charge_state
    assert _state(device).position_source is PositionSource.DOCK


@pytest.mark.parametrize(
    ("mode", "charge_state"),
    [
        (WorkMode.MODE_WORKING, 0),
        (WorkMode.MODE_RETURNING, 0),
        (WorkMode.MODE_PAUSE, 0),  # paused out on the lawn
        (WorkMode.MODE_READY, 0),  # idle off the dock
    ],
)
def test_mammotion_position_does_not_snap_off_the_dock(mode: WorkMode, charge_state: int) -> None:
    device = sample_device()
    device.report_data.dev.sys_status = int(mode)
    device.report_data.dev.charge_state = charge_state
    state = _state(device)
    assert state.position_source is PositionSource.RTK
    assert state.position is not None
    assert state.position.lat == pytest.approx(_raw_position_lat())


@pytest.mark.parametrize(
    ("rtk_status", "has_report", "expected"),
    [
        (4, True, PositionSource.RTK),  # fix
        (5, True, PositionSource.RTK),  # float
        (1, True, PositionSource.GNSS),  # single: no RTK fix
        (0, True, PositionSource.GNSS),  # none
        (4, False, PositionSource.UNKNOWN),  # no report yet: quality unknown
    ],
)
def test_mammotion_position_source_follows_the_fix(
    rtk_status: int, has_report: bool, expected: PositionSource
) -> None:
    device = sample_device()
    device.report_data.dev.sys_status = int(WorkMode.MODE_WORKING)
    device.report_data.dev.charge_state = 0
    device.report_data.rtk.status = rtk_status
    state = _state(device, has_report=has_report)
    assert state.position_source is expected
    assert state.position is not None
    assert state.position.lat == pytest.approx(_raw_position_lat())


def test_mammotion_docked_without_known_dock_keeps_the_raw_position() -> None:
    device = sample_device(with_dock=False)
    device.report_data.rtk.status = 1  # single
    state = _state(device)
    assert state.activity is MowerActivity.DOCKED
    assert state.position_source is PositionSource.GNSS
    assert state.rtk_fix is RtkFixStatus.NONE
    assert state.position is not None
    assert state.position.lat == pytest.approx(_raw_position_lat())
    assert mapping.dock_known(device) is False
    assert mapping.dock_position_of(device) is None
    assert mapping.map_of(device).dock is None


def test_mammotion_dock_buffer_sets_dock_and_true_origin() -> None:
    device = sample_device(with_dock=False)
    # before the buffer: PyMammotion's fallback origin without yaw
    fallback = mapping.geo_reference_of(device)
    assert fallback is not None
    assert fallback.rotation_deg == 0.0
    device.buffer(dock_buffer(rtk_yaw_deg=-20.0, dock_x=2.5, dock_y=-0.5, rotation_deg=135))
    assert mapping.dock_known(device)
    reference = mapping.geo_reference_of(device)
    assert reference is not None
    assert reference.rotation_deg == pytest.approx(-20.0)
    assert reference.origin_lat == pytest.approx(RTK_LAT_DEG, abs=1e-6)
    assert reference.origin_lon == pytest.approx(RTK_LON_DEG, abs=1e-6)
    dock = mapping.map_of(device).dock
    assert dock is not None
    assert dock.x == pytest.approx(2.5)
    assert dock.y == pytest.approx(-0.5)
    assert dock.heading_deg == pytest.approx(
        (90 - 135 + 20) % 360
    )  # compass 135 in the local frame
    position = mapping.dock_position_of(device)
    assert position is not None
    assert position.heading == pytest.approx(135.0)  # the compass course as reported


@pytest.mark.parametrize(
    ("man_run_speed", "travel_speed", "expected"),
    [
        (45, 0.4, 0.45),  # the reported speed (cm/s) wins, as in the app and Mammotion-HA
        (0, 0.4, 0.4),  # nothing reported yet: the configured speed
        (900, 0.4, 0.4),  # implausible report (9 m/s): the configured speed
        (-20, 0.0, None),  # neither plausible
        (500, 0.0, 5.0),  # 5 m/s is still plausible
    ],
)
def test_mammotion_speed_prefers_the_reported_man_run_speed(
    man_run_speed: int, travel_speed: float, expected: float | None
) -> None:
    device = sample_device()
    device.report_data.work.man_run_speed = man_run_speed
    device.mower_state.travel_speed = travel_speed
    speed = mapping.speed_of(device)
    if expected is None:
        assert speed is None
    else:
        assert speed == pytest.approx(expected)
    assert _state(device).speed_mps == speed


def test_mammotion_raw_carries_the_device_blade_counter_when_reported() -> None:
    device = sample_device()
    assert "blade_used_s" not in _state(device).raw
    assert "blade_used_warn_s" not in _state(device).raw
    blade = device.report_data.maintenance.blade_used_time
    blade.blade_used_time = 12 * 3600 + 30
    blade.blade_used_warn_time = 100 * 3600
    raw = _state(device).raw
    assert raw["blade_used_s"] == 12 * 3600 + 30
    assert raw["blade_used_warn_s"] == 100 * 3600
    blade.blade_used_warn_time = 0
    raw = _state(device).raw
    assert raw["blade_used_s"] == 12 * 3600 + 30 and "blade_used_warn_s" not in raw


def test_mammotion_raw_carries_the_device_battery_cycle_counter_when_reported() -> None:
    device = sample_device()
    assert "bat_cycles" not in _state(device).raw
    device.mower_state.charge_settings.bat_cycle_times = 140
    assert _state(device).raw["bat_cycles"] == 140
    device.report_data.maintenance.bat_cycles = 152
    assert _state(device).raw["bat_cycles"] == 152


def test_mammotion_raw_carries_cutter_geometry_and_position_type() -> None:
    device = sample_device()
    raw = _state(device).raw
    assert "cutter_offset_m" not in raw and "cutter_width_m" not in raw
    assert raw["pos_type"] == 0
    device.report_data.work.cutter_offset = 0.12
    device.report_data.work.cutter_width = 0.3
    device.location.position_type = 4
    raw = _state(device).raw
    assert raw["cutter_offset_m"] == pytest.approx(0.12)
    assert raw["cutter_width_m"] == pytest.approx(0.3)
    assert raw["pos_type"] == 4


def test_mammotion_capabilities_prefer_the_reported_cutting_width() -> None:
    device = MowerDevice(name="Luba-VP12345")

    def caps(d: MowerDevice) -> MowerCapabilities:
        return mapping.capabilities_of(
            d,
            device_name="Luba-VP12345",
            product_key="",
            manual_drive=False,
            manual_drive_blades=False,
        )

    assert caps(device).cutting_width_mm == 400  # family constant
    device.report_data.work.cutter_width = 0.3504
    assert caps(device).cutting_width_mm == 350
    assert mapping.reported_cutting_width_mm(device) == 350


def test_mammotion_position_heading_is_the_reported_compass_course() -> None:
    """No RTK reference needed: the orientation is a compass course (hardware, 2026-10-10)."""
    device = sample_device()
    device.location.RTK.latitude = 0.0
    device.location.RTK.longitude = 0.0
    device.location.orientation = 304
    position = mapping.position_of(device)
    assert position is not None and position.heading == pytest.approx(304.0)
    device.location.orientation = 360
    position = mapping.position_of(device)
    assert position is not None and position.heading == 0.0


def test_mammotion_error_code_and_message() -> None:
    device = sample_device()
    device.report_data.dev.sys_status = int(WorkMode.MODE_SYSTEM_ERROR)
    device.errors.err_code_list = [0, 0]
    state = _state(device)
    assert state.activity is MowerActivity.ERROR
    assert state.error_code == "mode_system_error"
    device.errors.err_code_list = [1005, 0]
    state = _state(device)
    assert state.error_code == "1005"
    assert state.error_message
    device.report_data.dev.sys_status = int(WorkMode.MODE_READY)
    assert _state(device).error_code is None  # the list is a log, not an active error


@pytest.mark.parametrize("code", sorted(mapping.LIFT_ERROR_CODES))
def test_mammotion_lift_error_codes_set_the_raw_lifted_flag(code: str) -> None:
    device = sample_device()
    device.report_data.dev.sys_status = int(WorkMode.MODE_SYSTEM_ERROR)
    device.errors.err_code_list = [int(code), 0]
    state = _state(device)
    assert state.error_code == code and state.raw["lifted"] is True
    device.errors.err_code_list = [1005, 0]
    assert "lifted" not in _state(device).raw  # another error is no lift
    device.errors.err_code_list = [int(code), 0]
    device.report_data.dev.sys_status = int(WorkMode.MODE_READY)
    assert "lifted" not in _state(device).raw  # the code in the log alone is no lift


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (4, RtkFixStatus.FIXED),
        (5, RtkFixStatus.FLOAT),
        (1, RtkFixStatus.NONE),
        (2, RtkFixStatus.NONE),
        (0, RtkFixStatus.NONE),
        (77, RtkFixStatus.UNKNOWN),
    ],
)
def test_mammotion_rtk_fix_from_report_status(status: int, expected: RtkFixStatus) -> None:
    device = sample_device()
    device.report_data.rtk.status = status
    assert mapping.rtk_fix_of(device) is expected


@pytest.mark.parametrize(
    ("switch", "expected"),
    [
        ("RTK_USED_NRTK", RtkSource.NETWORK),
        ("RTK_USED_LORA", RtkSource.BASE_STATION),
        ("RTK_USED_INTERNET", RtkSource.BASE_STATION),
        ("RTK_USED_SOMETHING_NEW", RtkSource.UNKNOWN),
    ],
)
def test_mammotion_rtk_source_from_rtk_switch(switch: str, expected: RtkSource) -> None:
    device = sample_device()
    device.report_data.rtk.mqtt_rtk_info.rtk_switch = switch
    assert mapping.rtk_source_of(device) is expected


def test_mammotion_rtk_is_unknown_before_the_first_report() -> None:
    device = MowerDevice(name="Luba-VP1")
    assert mapping.rtk_fix_of(device, has_report=False) is RtkFixStatus.UNKNOWN
    assert mapping.rtk_source_of(device, has_report=False) is RtkSource.UNKNOWN
    state = _state(device, has_report=False)
    assert state.activity is MowerActivity.UNKNOWN
    assert state.position is None


# --- capabilities ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "random_angle", "model", "width"),
    [
        ("Luba-VP12345", True, "Luba VP", 400),
        ("Luba-VS12345", True, "Luba 2", 400),
        ("Luba-MN12345", True, "HM430", None),
        ("Yuka-12345", True, "Yuka", 320),
        ("Luba-12345", False, "Luba 1", 400),
    ],
)
def test_mammotion_capability_table_per_device_type(
    name: str, random_angle: bool, model: str, width: int | None
) -> None:
    caps = mapping.capabilities_of(
        MowerDevice(name=name),
        device_name=name,
        product_key="",
        manual_drive=False,
        manual_drive_blades=True,
    )
    assert caps.model == model
    assert {"stripes", "diagonal", "checkerboard", "edge_only"} <= caps.native_patterns
    assert caps.supports_pattern(mapping.RANDOM_ANGLE) is random_angle
    assert caps.custom_paths is False
    assert caps.manual_drive is False and caps.manual_drive_blades is False
    assert caps.connection_kinds == {ConnectionKind.CLOUD, ConnectionKind.BLE_BRIDGE}
    assert caps.blade_height_range_mm is None  # unknown product: no name-only guess
    assert caps.max_speed_mps is None
    assert caps.cutting_width_mm == width
    assert mapping.cutting_width_of(name) == width


def test_mammotion_capability_limits_from_the_sub_model() -> None:
    caps = mapping.capabilities_of(
        sample_device(),
        device_name="Luba-VP1",
        product_key="a1iMygIwxFC",
        manual_drive=True,
        manual_drive_blades=True,
    )
    assert caps.model == "LUBA 2 AWD 5000X"
    assert caps.blade_height_range_mm == (25, 70)
    assert caps.path_spacing_range_mm == (200, 350)
    # the mowing speed (cutting_speed), not the travel speed (working_speed 0.2 to 0.8)
    assert caps.min_speed_mps == pytest.approx(0.2)
    assert caps.max_speed_mps == pytest.approx(0.6)
    assert caps.speed_allowed(0.6) and not caps.speed_allowed(0.1)
    assert caps.manual_drive and caps.manual_drive_blades
    assert caps.rtk_sources == {RtkSource.NETWORK, RtkSource.BASE_STATION}
    assert mapping.model_profile("Luba-1234").rtk_sources == {RtkSource.BASE_STATION}


def test_mammotion_capability_limits_unknown_until_the_sub_model_is_known() -> None:
    device = sample_device(sub_model_id="")
    caps = mapping.capabilities_of(
        device,
        device_name="Luba-VP1",
        product_key="a1iMygIwxFC",  # Luba VP: not in the table, no Yuka fallback
        manual_drive=False,
        manual_drive_blades=False,
    )
    assert caps.blade_height_range_mm is None
    assert caps.min_speed_mps is None and caps.max_speed_mps is None
    assert caps.path_spacing_range_mm is None
    assert not caps.speed_allowed(0.3)
    assert mapping.path_spacing_of(device, "a1iMygIwxFC") is None
    device.mower_state.sub_model_id = "HM030070LB2PAWD50OMNI"
    assert mapping.path_spacing_of(device, "a1iMygIwxFC") == (20, 35)


def test_mammotion_capability_limits_from_a_product_key_entry() -> None:
    # Product-key entries have no cutting speed: the working speed range is used instead.
    caps = mapping.capabilities_of(
        sample_device(sub_model_id="", product_key=""),
        device_name="Luba-1",
        product_key="a1nf9kRBWoH",
        manual_drive=False,
        manual_drive_blades=False,
    )
    assert caps.blade_height_range_mm == (30, 70)
    assert caps.min_speed_mps == pytest.approx(0.2)
    assert caps.max_speed_mps == pytest.approx(1.2)


@pytest.mark.parametrize(
    ("entry", "blade", "speed"),
    [
        (  # implausible speed dropped, blade kept
            {"blade_height_min": 25, "blade_height_max": 70, "cutting_speed_max": 9.0},
            (25, 70),
            None,
        ),
        (  # zero blade height (Yuka-style) means not settable
            {"blade_height_min": 0, "blade_height_max": 0, "working_speed_min": 0.2},
            None,
            None,
        ),
        (  # inverted or non-numeric values are ignored
            {
                "blade_height_min": 70,
                "blade_height_max": 25,
                "cutting_speed_min": "0.2",
                "cutting_speed_max": 0.6,
                "working_speed_min": 0.3,
                "working_speed_max": 0.5,
            },
            None,
            (0.3, 0.5),
        ),
    ],
)
def test_mammotion_model_limits_sanity(
    monkeypatch: pytest.MonkeyPatch,
    entry: dict[str, Any],
    blade: tuple[int, int] | None,
    speed: tuple[float, float] | None,
) -> None:
    entry.setdefault("cutting_speed_min", 0.2)
    monkeypatch.setattr(mapping, "model_config_of", lambda _device, _key="": entry)
    assert mapping.model_limits(sample_device()) == (blade, speed)


def test_mammotion_is_mower() -> None:
    assert mapping.is_mower("Luba-VP1")
    assert not mapping.is_mower("RTK12345")
    assert not mapping.is_mower("Spino-S1abc")


# --- map ------------------------------------------------------------------------------------


def test_mammotion_map_areas_zones_paths_dock_and_reference() -> None:
    mower_map = mapping.map_of(sample_device())
    assert mower_map.area_ids == [str(FRONT_HASH), str(BACK_HASH)]
    front = mower_map.area(str(FRONT_HASH))
    assert front is not None and front.name == "Front lawn"
    assert front.boundary == [(0, 0), (12, 0), (12, 8), (0, 8)]  # two frames, in order
    assert [z.id for z in mower_map.no_go_zones] == [str(NOGO_HASH)]
    assert [z.id for z in mower_map.obstacles] == [str(OBSTACLE_HASH)]
    assert [p.id for p in mower_map.paths] == [str(PATH_HASH)]
    assert mower_map.dock is not None
    assert (mower_map.dock.x, mower_map.dock.y) == (-1.0, 1.0)
    assert mower_map.revision == "424242"
    ref = mower_map.reference
    assert ref.origin_lat == pytest.approx(RTK_LAT_DEG)
    assert ref.origin_lon == pytest.approx(RTK_LON_DEG)
    assert ref.rotation_deg == pytest.approx(RTK_YAW_DEG)


def test_mammotion_map_matches_pymammotion_geojson_conversion() -> None:
    """Our reference reproduces PyMammotion's own local-to-WGS84 conversion (round trip)."""
    from pymammotion.data.model.generate_geojson import GeojsonGenerator
    from shapely.geometry import Point

    mower_map = mapping.map_of(sample_device())
    rtk = Point(RTK_LAT_DEG, RTK_LON_DEG)  # PyMammotion's convention: x = lat, y = lon
    for x, y in [(12.0, 8.0), (-5.0, 30.0)]:
        lon, lat = GeojsonGenerator.lon_lat_delta(rtk, x, y, math.radians(RTK_YAW_DEG))
        ours = to_wgs84(mower_map, x, y)
        assert ours[0] == pytest.approx(lat, abs=1e-7)
        assert ours[1] == pytest.approx(lon, abs=1e-7)
        assert from_wgs84(mower_map, *ours) == pytest.approx((x, y), abs=1e-6)


def test_mammotion_map_skips_incomplete_and_degenerate_elements() -> None:
    device = sample_device()
    device.map.area[7] = frames(PathType.AREA, 7, [(0, 0), (1, 0), (1, 1), (0, 1)], parts=2)
    device.map.area[7].data.pop()  # one of two frames missing
    device.map.area[8] = frames(PathType.AREA, 8, [(0, 0), (1, 0)])  # not a polygon
    device.map.path[9] = frames(PathType.PATH, 9, [(0, 0)])
    device.map.no_go_zone[11] = frames(PathType.NO_GO_ZONE, 11, [(0, 0), (1, 0)])
    device.map.no_go_zone_variant[10] = frames(
        PathType.NO_GO_ZONE_VARIANT, 10, [(0, 0), (1, 0), (1, 1)]
    )
    mower_map = mapping.map_of(device)
    assert "7" not in mower_map.area_ids and "8" not in mower_map.area_ids
    assert "9" not in [p.id for p in mower_map.paths]
    assert [z.id for z in mower_map.no_go_zones] == [str(NOGO_HASH), "10"]
    assert not mapping.map_complete(device)


def test_mammotion_map_without_reference_dock_or_revision() -> None:
    device = sample_device()
    device.location.RTK.latitude = 0.0
    device.location.RTK.longitude = 0.0
    device.location.dock.latitude = 0.0
    device.location.dock.longitude = 0.0
    device.report_data.locations = []
    device.location.RTK.yaw = math.radians(350.0)
    mower_map = mapping.map_of(device)
    assert (mower_map.reference.origin_lat, mower_map.reference.origin_lon) == (0.0, 0.0)
    assert mower_map.dock is None and mower_map.revision is None
    assert mapping.map_complete(device)
    assert not mapping.map_complete(MowerDevice(name="Luba-VP1"))
    device.location.RTK.latitude = 0.1
    ref = mapping.geo_reference_of(device)
    assert ref is not None and ref.rotation_deg == pytest.approx(-10.0)


@pytest.mark.parametrize(
    ("lat", "lon", "yaw"),
    [(5.3, 0.2, 0.0), (0.8, -4.0, 0.0), (math.nan, 0.2, 0.0), (0.8, 0.2, math.inf)],
)
def test_mammotion_an_impossible_rtk_origin_is_no_reference(
    lat: float, lon: float, yaw: float
) -> None:
    """Found by fuzzing (fuzz/fuzz_mammotion_report.py): a corrupt origin (radians beyond the
    Earth's range) failed GeoReference validation, so map_of and state_of raised."""
    device = sample_device()
    device.location.RTK.latitude = lat
    device.location.RTK.longitude = lon
    device.location.RTK.yaw = yaw
    assert mapping.geo_reference_of(device) is None
    assert mapping.dock_position_of(device) is None
    mapping.map_of(device)
    _state(device)


def test_mammotion_raw_survives_an_unset_position_type() -> None:
    """Found by fuzzing: a location whose pos_type arrives with the wrong protobuf wire type
    is decoded as None by PyMammotion, and int(None) stopped the state update."""
    device = sample_device()
    device.location.position_type = None  # type: ignore[assignment]
    assert _state(device).raw["pos_type"] == 0


def test_mammotion_state_survives_unset_numeric_fields() -> None:
    """Found by fuzzing: the same for the fields PyMammotion copies from other messages."""
    device = sample_device()
    device.mower_state.travel_speed = None  # type: ignore[assignment]
    device.mower_state.turning_mode = None  # type: ignore[assignment]
    device.report_data.work.knife_height = None  # type: ignore[assignment]
    device.report_data.dev.battery_val = None  # type: ignore[assignment]
    device.report_data.dev.sys_status = None  # type: ignore[assignment]
    state = _state(device)
    assert state.battery_percent == 0
    assert state.blade_height_mm is None
    assert state.drive_speed_mps is None


def test_mammotion_area_summary() -> None:
    summary = mapping.area_summary(mapping.map_of(sample_device()))
    assert [(s["name"], s["size_m2"]) for s in summary] == [
        ("Front lawn", 96.0),
        ("Back lawn", 60.0),
    ]
    assert summary[0]["centre"][0] == pytest.approx(RTK_LAT_DEG, abs=1e-3)


# --- commands -------------------------------------------------------------------------------


def _caps() -> MowerCapabilities:
    return mapping.capabilities_of(
        sample_device(),
        device_name="Luba-VP1",
        product_key="a1nf9kRBWoH",
        manual_drive=False,
        manual_drive_blades=False,
    )


def _plan(pattern: dict[str, Any] | None) -> mapping.RoutePlan:
    command = StartMow(area_ids=[str(FRONT_HASH)], pattern=pattern)
    return mapping.route_plan(
        command, _caps(), blade_height_mm=55, speed_mps=0.3, path_spacing_cm=(20, 35)
    )


@pytest.mark.parametrize(
    ("pattern", "channel_mode", "toward", "toward_mode"),
    [
        (None, CuttingMode.single_grid, 0, PathAngleSetting.absolute_angle),
        ({"name": "stripes", "angle_deg": 30}, CuttingMode.single_grid, 30, 1),
        ({"name": "diagonal", "angle_deg": 30}, CuttingMode.single_grid, 75, 1),
        ({"name": "checkerboard", "angle_deg": 10}, CuttingMode.double_grid, 10, 1),
        ({"name": "edge_only", "border_laps": 2}, CuttingMode.no_grid, 0, 1),
        ({"name": "random_angle"}, CuttingMode.single_grid, 0, PathAngleSetting.random_angle),
        ({"name": "stripes", "angle_mode": "relative"}, 0, 0, PathAngleSetting.relative_angle),
        ({"name": "stripes", "angle_mode": "random"}, 0, 0, PathAngleSetting.random_angle),
    ],
)
def test_mammotion_pattern_settings_mapping(
    pattern: dict[str, Any] | None, channel_mode: int, toward: int, toward_mode: int
) -> None:
    plan = _plan(pattern)
    settings = plan.settings
    assert plan.zone_hashes == (FRONT_HASH,)
    assert settings.areas == [FRONT_HASH]
    assert settings.channel_mode == channel_mode
    assert settings.toward == toward
    assert settings.toward_mode == toward_mode
    assert settings.channel_width == 25
    assert settings.blade_height == 55


def test_mammotion_pattern_extras_and_border_laps() -> None:
    plan = _plan(
        {
            "name": "stripes",
            "spacing_m": 0.3,
            "border_laps": 3,
            "obstacle_laps": 2,
            "blade_height_mm": 45,
            "speed_mps": 0.5,
            "order": "grid_first",
        }
    )
    s = plan.settings
    assert (s.channel_width, s.mowing_laps, s.border_mode, s.obstacle_laps) == (30, 3, 3, 2)
    assert (s.blade_height, s.speed) == (45, 0.5)


def test_mammotion_start_sheet_pattern_reaches_the_route_plan() -> None:
    # The web start sheet sends a ``MowPattern``: border laps and angle must reach the mower.
    pattern = MowPattern(name="stripes", angle_deg=45, border_laps=2)
    command = StartMow.model_validate(
        {
            "kind": "start_mow",
            "area_ids": [str(FRONT_HASH)],
            "pattern": pattern.model_dump(mode="json", exclude_none=True),
        }
    )
    plan = mapping.route_plan(
        command, _caps(), blade_height_mm=55, speed_mps=0.3, path_spacing_cm=(20, 35)
    )
    s = plan.settings
    assert (s.toward, s.toward_mode) == (45, PathAngleSetting.absolute_angle)
    assert (s.mowing_laps, s.border_mode) == (2, 2)
    assert plan.job.border_laps == 2
    assert plan.job.passes == (45.0,)
    assert s.channel_width == 25  # no spacing given: the default 0.25 m


@pytest.mark.parametrize(
    "pattern",
    [
        {"name": "spiral"},
        {"name": "stripes", "spacing_m": 0.5},
        {"name": "stripes", "obstacle_laps": 9},
        {"name": "stripes", "angle_mode": "sideways"},
        {"name": "stripes", "speed_mps": 0},
        {"name": "stripes", "border_laps": 7},
    ],
)
def test_mammotion_invalid_pattern_settings(pattern: dict[str, Any]) -> None:
    with pytest.raises((ValueError, UnsupportedPattern)):
        _plan(pattern)


def test_mammotion_area_ids_must_be_zone_hashes() -> None:
    with pytest.raises(ValueError, match="zone hashes"):
        mapping.route_plan(StartMow(area_ids=["front"]), _caps(), blade_height_mm=55, speed_mps=0.3)


@pytest.mark.parametrize(
    ("linear", "angular", "wire"),
    [
        (0.0, 0.0, (0, 0)),
        (0.1, -0.1, (0, 0)),  # dead zone
        (1.0, 0.0, (850, 0)),
        (-0.5, 0.0, (-350, 0)),
        (0.0, 1.0, (0, -382)),  # left is negative on the wire, as move_left sends it
        (0.0, -0.2, (0, 22)),
        (2.0, 0.0, (850, 0)),  # clamped
    ],
)
def test_mammotion_movement_wire_values(
    linear: float, angular: float, wire: tuple[int, int]
) -> None:
    assert mapping.movement_wire(linear, angular) == wire


def test_mammotion_movement_wire_agrees_with_pymammotion_move_builders() -> None:
    from pymammotion.utility.movement import get_percent, transform_both_speeds

    forward = transform_both_speeds(90.0, 0.0, get_percent(60.0), 0.0)
    left = transform_both_speeds(0.0, 180.0, 0.0, get_percent(60.0))
    assert mapping.movement_wire(0.6, 0.0) == tuple(forward)
    assert mapping.movement_wire(0.0, 0.6) == tuple(left)


def test_mammotion_model_is_surfaced_in_raw_and_capabilities() -> None:
    assert "model" not in _state(sample_device()).raw
    assert _state(sample_device(), model="LubaAWD1000723").raw["model"] == "LubaAWD1000723"

    def caps(model: str = "") -> str | None:
        return mapping.capabilities_of(
            sample_device(),
            device_name="Luba-VP1",
            product_key="a1nf9kRBWoH",
            manual_drive=False,
            manual_drive_blades=False,
            model=model,
        ).model

    assert caps() == "LUBA 2 AWD 5000X"  # exact model from the sub model's table entry
    assert caps("LubaAWD1000723") == "LubaAWD1000723"


@pytest.mark.parametrize(
    ("turning_mode", "expected"),
    [(0, TurnStyle.IN_PLACE), (1, TurnStyle.MULTI_POINT), (7, None)],
)
def test_mammotion_turning_mode_is_the_mowers_turn_style(
    turning_mode: int, expected: TurnStyle | None
) -> None:
    """ADR 0043: ``mower_state.turning_mode`` (0 zero-turn, 1 multipoint), read only."""
    device = sample_device()
    device.mower_state.turning_mode = turning_mode
    assert mapping.turn_style_of(device) is expected
    assert _state(device).turn_style is expected
    assert device.mower_state.turning_mode == turning_mode  # never changed


def test_mammotion_drive_reverse_follows_the_bluetooth_drive_session() -> None:
    device = MowerDevice(name="Luba-VP12345")

    def caps(manual_drive: bool) -> MowerCapabilities:
        return mapping.capabilities_of(
            device,
            device_name="Luba-VP12345",
            product_key="",
            manual_drive=manual_drive,
            manual_drive_blades=False,
        )

    assert caps(True).drive_reverse  # send_movement takes a negative linear speed
    assert not caps(False).drive_reverse
    assert mapping.movement_wire(-1.0, 0.0)[0] < 0


# --- typed job, GNSS, signal and self-check ---------------------------------------------------


def _packed(high: int, low: int) -> int:
    return (high << 16) | low


def test_job_of_a_running_job_unpacks_progress_area_and_minutes() -> None:
    device = sample_device()
    device.report_data.dev.sys_status = int(WorkMode.MODE_WORKING)
    device.report_data.dev.charge_state = 0
    device.report_data.work.area = _packed(42, 310)  # 42 %, 310 m2 mowed
    device.report_data.work.progress = _packed(30, 51)  # 30 min left of 51
    device.location.work_zone = FRONT_HASH
    state = _state(device)
    assert state.job is not None
    assert state.job.progress_percent == 42
    assert state.job.mowed_m2 == 310.0
    assert (state.job.elapsed_s, state.job.remaining_s) == (21 * 60, 30 * 60)
    assert state.job.area_id == str(FRONT_HASH)
    # The raw keys stay for older consumers.
    assert state.raw["progress_percent"] == 42 and state.raw["work_zone"] == FRONT_HASH


def test_job_of_unknown_zone_and_missing_estimate_stay_null() -> None:
    device = sample_device()
    device.report_data.dev.sys_status = int(WorkMode.MODE_PAUSE)
    device.report_data.dev.charge_state = 0
    device.location.work_zone = 987654321  # not an area of the map
    job = mapping.job_of(device)
    assert job is not None and job.area_id is None
    assert job.elapsed_s is None and job.remaining_s is None and job.progress_percent == 0


@pytest.mark.parametrize("mode", [WorkMode.MODE_READY, WorkMode.MODE_CHARGING])
def test_job_of_without_a_job_is_none(mode: WorkMode) -> None:
    device = sample_device()
    device.report_data.dev.sys_status = int(mode)
    device.report_data.work.area = _packed(100, 500)  # left over from the last job
    assert _state(device).job is None
    assert mapping.job_of(device, has_report=False) is None


def test_gnss_signal_and_self_check_of_the_sample_device() -> None:
    from mowdeck_contracts.v1 import GnssSolution, NetworkKind, SelfCheckResult

    state = _state(sample_device())
    assert state.gnss is not None
    assert (state.gnss.satellites, state.gnss.correction_age_s) == (31, 1.0)
    assert state.gnss.rtk_status is GnssSolution.FIXED
    assert state.signal is not None
    assert (state.signal.wifi_dbm, state.signal.mobile_dbm) == (None, -79)
    assert state.signal.network is NetworkKind.MOBILE
    assert state.self_check is not None and state.self_check.result is SelfCheckResult.OK
    assert state.self_check.detail is None and state.error_category is None


def test_gnss_signal_and_self_check_are_null_before_the_first_report() -> None:
    state = _state(sample_device(), has_report=False)
    assert state.gnss is None and state.signal is None and state.self_check is None
    assert state.job is None


@pytest.mark.parametrize(
    ("status", "expected"),
    [(0, "none"), (1, "single"), (2, "single"), (4, "fixed"), (5, "float"), (9, "unknown")],
)
def test_gnss_solution(status: int, expected: str) -> None:
    device = sample_device()
    device.report_data.rtk.status = status
    device.report_data.rtk.age = 0
    gnss = mapping.gnss_of(device)
    assert gnss is not None and gnss.rtk_status == expected
    assert gnss.correction_age_s is None  # age 0: no corrections


@pytest.mark.parametrize(
    ("used_net", "connect_type", "ble_rssi", "expected"),
    [
        ("NET_USED_TYPE_WIFI", 0, 0, "wifi"),
        ("WIFI", 0, 0, "wifi"),
        ("NET_USED_TYPE_MNET", 0, 0, "mobile"),
        ("NET_USED_TYPE_TC_MNET", 0, 0, "mobile"),
        ("NONE", 2, 0, "wifi"),
        ("NONE", 1, 0, "mobile"),
        ("NONE", 0, -70, "ble"),
        ("NONE", 0, 0, "none"),
    ],
)
def test_network_of(used_net: str, connect_type: int, ble_rssi: int, expected: str) -> None:
    device = sample_device()
    connect = device.report_data.connect
    connect.used_net, connect.connect_type, connect.ble_rssi = used_net, connect_type, ble_rssi
    assert mapping.network_of(device) == expected


@pytest.mark.parametrize(("reported", "dbm"), [(-55, -55), (0, None), (12, None), (-200, None)])
def test_signal_dbm_drops_unreported_and_implausible_values(reported: int, dbm: int | None) -> None:
    device = sample_device()
    device.report_data.connect.wifi_rssi = reported
    signal = mapping.signal_of(device)
    assert signal is not None and signal.wifi_dbm == dbm


def test_self_check_blocked_names_the_reason() -> None:
    from mowdeck_contracts.v1 import SelfCheckResult

    device = sample_device()
    device.report_data.dev.self_check_status = 20
    state = _state(device)
    assert state.self_check is not None and state.self_check.result is SelfCheckResult.BLOCKED
    assert state.self_check.detail == "rain" and state.raw["self_check"] == "rain"
