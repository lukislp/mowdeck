"""Pure translation between PyMammotion data structures and MowDeck contracts.

Nothing here does I/O. The functions read the real PyMammotion data classes (re-exported by
:mod:`mowdeck_control_mammotion.port`), so the tests build those classes and a renamed field in a
PyMammotion upgrade breaks them.

Frames: PyMammotion keeps map geometry and the mower position as ``(x, y)`` metres in a
device-local frame around the RTK reference point, rotated by the RTK yaw. East and north are
``east = x cos(yaw) - y sin(yaw)`` and ``north = x sin(yaw) + y cos(yaw)``
(``MowerDevice.update_report_data`` and ``GeojsonGenerator.lon_lat_delta``). That is exactly the
:class:`~mowdeck_contracts.v1.GeoReference` convention with ``rotation_deg = degrees(yaw)``, so
map points are copied unchanged and the reference is the RTK origin (stored in radians).
"""

import logging
import math
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final
from uuid import UUID

from mowdeck_contracts.v1 import (
    BaseStationPosition,
    BaseStationRef,
    BaseStationState,
    CameraAccess,
    CameraEncryption,
    CameraInfo,
    CameraPlacement,
    CameraProvider,
    CameraQuota,
    CameraSession,
    ConnectionKind,
    DockPose,
    DriveSpeedSetting,
    FirmwareInfo,
    GeoPosition,
    GeoReference,
    GnssSolution,
    GnssStatus,
    JobProgress,
    LinkStatus,
    LocalPoint,
    MaintenanceAction,
    MapArea,
    MapPath,
    MapZone,
    MowerActivity,
    MowerCapabilities,
    MowerErrorCategory,
    MowerMap,
    MowerState,
    MowPath,
    MowPathKind,
    MowPathProgress,
    NetworkKind,
    ObstacleDetection,
    PathOrder,
    PositionSource,
    ProxyInfo,
    RtkCorrectionLink,
    RtkFixStatus,
    RtkSource,
    SelfCheck,
    SelfCheckResult,
    SettingSpec,
    SettingValue,
    SignalStatus,
    StartMow,
    StartOptions,
    TurnStyle,
)
from mowdeck_contracts.v1 import DeviceType as DiscoveredType
from mowdeck_control.geo import to_wgs84
from mowdeck_control.translation import MowRequest, NativeJob, Pattern, plan_to_native_job

from mowdeck_control_mammotion import errors
from mowdeck_control_mammotion.port import (
    FIRMWARE_PLACEHOLDER,
    CheckDeviceVersion,
    CoordinateConverter,
    CuttingMode,
    DetectionStrategy,
    DeviceType,
    FrameList,
    MowerDevice,
    MowOrder,
    MowPathPacket,
    OperationSettings,
    OTAProgress,
    PathAngleSetting,
    RTKStatus,
    StreamSubscriptionResponse,
    VideoResourceResponse,
    WorkMode,
    decode_agora_token,
    describe_error,
    model_config_of,
    model_of,
)

_log = logging.getLogger(__name__)

RANDOM_ANGLE: Final = "random_angle"
"""Extra native pattern name of Mammotion mowers: stripes at a random angle per job."""

CONNECTION_KINDS: Final = frozenset({ConnectionKind.CLOUD, ConnectionKind.BLE_BRIDGE})

_ERROR_MODES: Final = frozenset(
    {
        WorkMode.MODE_LOCK,
        WorkMode.MODE_SYSTEM_ERROR,
        WorkMode.MODE_LOCATION_ERROR,
        WorkMode.MODE_BOUNDARY_JUMP,
    }
)
_ACTIVITY_BY_MODE: Final[Mapping[WorkMode, MowerActivity]] = {
    WorkMode.MODE_WORKING: MowerActivity.MOWING,
    WorkMode.MODE_RETURNING: MowerActivity.RETURNING,
    WorkMode.MODE_CORRIDOR_RETURNING: MowerActivity.RETURNING,
    WorkMode.MODE_PAUSE: MowerActivity.PAUSED,
    WorkMode.MODE_CHARGING_PAUSE: MowerActivity.PAUSED,
    WorkMode.MODE_CORRIDOR_PAUSE: MowerActivity.PAUSED,
    WorkMode.MODE_CHARGING: MowerActivity.CHARGING,
    WorkMode.MODE_OFFLINE: MowerActivity.OFFLINE,
    WorkMode.MODE_POWER_OFF: MowerActivity.OFFLINE,
    WorkMode.MODE_SLEEPING: MowerActivity.OFFLINE,
    WorkMode.MODE_UNCONNECTED: MowerActivity.OFFLINE,
    WorkMode.MODE_DISABLE: MowerActivity.OFFLINE,
    WorkMode.MODE_UPDATING: MowerActivity.UPDATING,
    **dict.fromkeys(_ERROR_MODES, MowerActivity.ERROR),
}
"""Work modes with a fixed activity. Every other known mode is ``idle`` (or ``docked`` on the
charger); ``MODE_NOT_ACTIVE`` and unknown values are ``unknown``."""

_RTK_FIX: Final[Mapping[RTKStatus, RtkFixStatus]] = {
    RTKStatus.FIX: RtkFixStatus.FIXED,
    RTKStatus.FLOAT: RtkFixStatus.FLOAT,
    RTKStatus.SINGLE: RtkFixStatus.NONE,
    RTKStatus.NONE: RtkFixStatus.NONE,
}
_RTK_SOURCE: Final[Mapping[str, RtkSource]] = {
    "RTK_USED_NRTK": RtkSource.NETWORK,  # network RTK (virtual reference station)
    "RTK_USED_LORA": RtkSource.BASE_STATION,  # own base station over LoRa
    "RTK_USED_INTERNET": RtkSource.BASE_STATION,  # own base station over the internet
}

LIFT_ERROR_CODES: Final = frozenset(str(code) for code in errors.LIFT_TILT_CODES)
"""Error codes of PyMammotion's error table that mean the mower was lifted or tilted: 1004 tilt
sensor activated, 1030 front wheels lifted or tilted, 2502 machine tilted too much, 2801 lift
sensor triggered, 11131 and 11133 rear lift sensor triggered while reversing or turning. A state
in error with a ``lift_tilt`` code (:mod:`~mowdeck_control_mammotion.errors`) carries
``raw["lifted"] = True`` (the theft alarm of Notifications reads that flag, so it never needs
this GPL package)."""

FAULT_TOLERANCE_S: Final = 120.0
"""A fault logged (or a warning received) up to this long before the adapter saw the mower pause
or fail still counts as its cause: the work mode report lags the error log and the events."""
FAULT_LOOKBACK_S: Final = 1800.0
"""The same when the mower already stood paused or failed at connect (the start is unknown)."""
FaultWarning = tuple[int, datetime]
"""A warning code the mower posted (``thing/event``) with the adapter's receive time."""

LINEAR_WIRE_PER_PERCENT: Final = 10.0
ANGULAR_WIRE_PER_PERCENT: Final = 4.5
DEAD_ZONE_PERCENT: Final = 15.0
"""Movement scaling of ``MammotionCommand.move_forward`` / ``move_left``
(``utility/movement.py``): input percent minus a 15 % dead zone, times 10 (linear) or 4.5
(angular). Full stick forward is 850, full turn 382. PyMammotion's ``move_left`` sends a
negative angular value, so MowDeck's "left is positive" is negated on the wire.

Checked against PyMammotion 0.10.9 (ADR 0042): ``move_forward(1.0)`` runs
``get_percent(100) = 85`` through ``RockerControlUtil.transfrom3(90, 85) = [85, 0]`` and
``transform_both_speeds`` (``linear = 85 * 10 = 850``), ``move_left(1.0)`` gives
``transfrom3(180, 85) = [0, -85]`` and ``int(-85 * 4.5) = -382``; ``mammotion/control.py`` uses
the same helpers. The cloud remote drive (``device/remote_drive.py``, ``session_ctrl``) takes
the caller's values in the same wire units unchanged and documents no other range, so nothing
in PyMammotion sends more than 850 / 382 and these constants match it. The ground speed of a
given wire value is not fixed by them: the mower scales the stick by the vendor's speed setting
for manual driving (``MowerState.drive_speed_mps``), and the drive calibration measures the
result (``DriveCalibrate``)."""

DRIVE_SPEED_DEFAULT_RANGE_MPS: Final = (0.2, 1.2)
DRIVE_SPEED_STEP_MPS: Final = 0.1
"""The speed setting for manual driving (``MowerCapabilities.drive_speed_setting``, ADR 0042):
read with ``get_speed`` and changed with ``set_speed`` (``MctlDriver.bidire_speed_read_set``,
``DrvSrSpeed``; the reply sets ``mower_state.travel_speed``). The exact range of the app's
manual-driving speed is not verified on hardware: it is taken from the model's travel speed
limits in PyMammotion's table (``working_speed_min/max``, 0.2 to 1.2 m/s for most models), else
the model's speed range, else this default. ``SetSpeed`` writes the same message, so the
mowing speed and this setting may well be one value on the mower."""


# --- State ------------------------------------------------------------------------------------


def work_mode(device: MowerDevice) -> WorkMode:
    """The device's work mode (``UNKNOWN`` for unmodelled values)."""
    return WorkMode(_int(device.report_data.dev.sys_status))


def activity_of(device: MowerDevice, *, has_report: bool = True) -> MowerActivity:
    """Map the work mode, charger state and online flag to a :class:`MowerActivity`."""
    if not device.online:
        return MowerActivity.OFFLINE
    if not has_report:
        return MowerActivity.UNKNOWN
    mode = work_mode(device)
    fixed = _ACTIVITY_BY_MODE.get(mode)
    if fixed is not None:
        return fixed
    if mode in (WorkMode.UNKNOWN, WorkMode.MODE_NOT_ACTIVE):
        return MowerActivity.UNKNOWN
    if device.report_data.dev.charge_state != 0:
        return MowerActivity.DOCKED
    return MowerActivity.IDLE


def rtk_fix_of(device: MowerDevice, *, has_report: bool = True) -> RtkFixStatus:
    """RTK fix from ``report_data.rtk.status`` (PyMammotion's ``RTKStatus``)."""
    if not has_report:
        return RtkFixStatus.UNKNOWN
    return _RTK_FIX.get(RTKStatus.from_value(device.report_data.rtk.status), RtkFixStatus.UNKNOWN)


def rtk_source_of(device: MowerDevice, *, has_report: bool = True) -> RtkSource:
    """Correction source from ``report_data.rtk.mqtt_rtk_info.rtk_switch`` (NRTK is network)."""
    if not has_report:
        return RtkSource.UNKNOWN
    return _RTK_SOURCE.get(device.report_data.rtk.mqtt_rtk_info.rtk_switch, RtkSource.UNKNOWN)


def geo_reference_of(device: MowerDevice) -> GeoReference | None:
    """The map frame anchor: the RTK reference (radians in PyMammotion) and its yaw.

    None while the mower has not reported a reference yet (latitude and longitude 0), and for a
    reference no Earth position has (a corrupt frame: beyond +-90/180 degrees, not finite).
    """
    rtk = device.location.RTK
    if rtk.latitude == 0.0 and rtk.longitude == 0.0:
        return None
    if not all(math.isfinite(value) for value in (rtk.latitude, rtk.longitude, rtk.yaw)):
        return None
    lat, lon = math.degrees(rtk.latitude), math.degrees(rtk.longitude)
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    rotation = math.degrees(rtk.yaw) % 360.0
    if rotation > 180.0:
        rotation -= 360.0
    return GeoReference(origin_lat=lat, origin_lon=lon, rotation_deg=rotation)


def position_of(device: MowerDevice) -> GeoPosition | None:
    """WGS84 position from ``location.device`` (degrees), heading from ``location.orientation``.

    The orientation is a compass course (degrees, 0 = north, clockwise), as PyMammotion's HA
    device tracker publishes it: verified on a LUBA 2 AWD X on 2026-10-10, where it matched
    the travel bearing of the RTK-fixed track when driving out and was opposite while backing
    into the dock.
    """
    point = device.location.device
    if point.latitude == 0.0 and point.longitude == 0.0:
        return None
    if not (-90.0 <= point.latitude <= 90.0 and -180.0 <= point.longitude <= 180.0):
        return None
    heading = float(device.location.orientation) % 360.0
    return GeoPosition(lat=point.latitude, lon=point.longitude, heading=heading)


def dock_known(device: MowerDevice) -> bool:
    """True once the RTK origin and the dock pose arrived (``system_update_buf`` type 1).

    Both come from the buffer the mower sends in answer to ``read_write_device(rw_id=5,
    context=1, rw=1)``; without it the dock is ``(0, 0)`` and the origin either unset or
    PyMammotion's fallback from ``mqtt_rtk_info`` (without yaw).
    """
    rtk = device.location.RTK
    return dock_of(device) is not None and not (rtk.latitude == 0.0 and rtk.longitude == 0.0)


def dock_position_of(device: MowerDevice) -> GeoPosition | None:
    """The charging station in WGS84 with its heading, None while dock or origin are unknown.

    The same forward transform as PyMammotion's mower position
    (``MowerDevice.update_report_data``): the local ``(x, y)`` of the dock (``dock.longitude``
    and ``dock.latitude``, metres) rotated by the RTK yaw to east and north, then
    ``CoordinateConverter(origin).enu_to_lla(north, east)`` (PyMammotion's argument order). The
    heading reads ``dock.rotation`` as a compass course (the dock's exit direction), like
    :func:`dock_of`; unverified on hardware.
    """
    reference = geo_reference_of(device)
    dock = dock_of(device)
    if reference is None or dock is None:
        return None
    rtk = device.location.RTK
    x, y = dock.x, dock.y
    east = math.cos(rtk.yaw) * x - math.sin(rtk.yaw) * y
    north = math.sin(rtk.yaw) * x + math.cos(rtk.yaw) * y
    point = CoordinateConverter(rtk.latitude, rtk.longitude).enu_to_lla(north, east)
    lat, lon = float(point.latitude), float(point.longitude)
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    heading = float(device.location.dock.rotation) % 360.0
    return GeoPosition(lat=lat, lon=lon, heading=heading)


_IN_DOCK: Final = frozenset({MowerActivity.DOCKED, MowerActivity.CHARGING})


def in_dock(device: MowerDevice, activity: MowerActivity) -> bool:
    """True while the mower stands in the charging station: activity ``docked`` or
    ``charging``, or a job paused on the charger (``charge_state != 0``)."""
    if activity in _IN_DOCK:
        return True
    return activity is MowerActivity.PAUSED and device.report_data.dev.charge_state != 0


def located_position_of(
    device: MowerDevice, activity: MowerActivity, rtk_fix: RtkFixStatus
) -> tuple[GeoPosition | None, PositionSource]:
    """The position to report and where it comes from.

    In the dock with a known dock pose: the dock (``dock``), because the docked mower often has
    no RTK fix and its raw position scatters by metres (the vendor app shows it at the dock).
    Otherwise the raw position, ``rtk`` with a fixed or float RTK fix, ``gnss`` without one,
    ``unknown`` when the fix is unknown or there is no position.
    """
    if in_dock(device, activity):
        dock = dock_position_of(device)
        if dock is not None:
            return dock, PositionSource.DOCK
    position = position_of(device)
    if position is None:
        return None, PositionSource.UNKNOWN
    match rtk_fix:
        case RtkFixStatus.FIXED | RtkFixStatus.FLOAT:
            return position, PositionSource.RTK
        case RtkFixStatus.NONE:
            return position, PositionSource.GNSS
        case _:
            return position, PositionSource.UNKNOWN


def fault_window_open(device: MowerDevice, activity: MowerActivity) -> bool:
    """True while a fault may hold the mower: an error mode, or a pause off the charger (a
    trapped or blocked mower reports ``MODE_PAUSE``, not an error mode)."""
    if activity is MowerActivity.ERROR:
        return True
    return activity is MowerActivity.PAUSED and not in_dock(device, activity)


@dataclass(frozen=True, slots=True)
class Fault:
    """The fault that holds the mower: the code and where it came from (``error_log``: the
    mower's error list, ``warning_event``: a ``thing/event`` warning post)."""

    code: int
    source: str


def fault_of(
    device: MowerDevice, since: datetime | None, warnings: Sequence[FaultWarning] = ()
) -> Fault | None:
    """The newest fault logged at or after ``since``, None when there is none (or no ``since``).

    Mammotion-HA reads the same sources: the newest entry of the error list
    (``errors.err_code_list``, filled by the error log read ``read_write_device(rw_id=5,
    context=2)``) and the warning events. The list is a log of the last ten faults, so only an
    entry with a real time at or after ``since`` counts (an entry still on its uptime stamp is
    picked up once the firmware dated it).
    """
    if since is None:
        return None
    best: tuple[datetime, Fault] | None = None
    dated = device.errors.dated_codes
    if dated:
        code, logged = dated[0]
        number = errors.normalise_code(code)
        if logged >= since and number:
            best = (logged, Fault(number, "error_log"))
    for code, received in warnings:
        if received >= since and (best is None or received > best[0]):
            best = (received, Fault(code, "warning_event"))
    return best[1] if best is not None else None


def error_of(
    device: MowerDevice, activity: MowerActivity, fault: Fault | None = None
) -> tuple[str | None, str | None]:
    """``(error_code, error_message)`` while the mower reports an error, else ``(None, None)``.

    The code is ``fault`` when given, else the newest entry of the device's error list
    (``errors.active_codes``, sign dropped) or, when the list is empty, the work mode name. The
    list is a log, so it is only read in an error.
    """
    if activity is not MowerActivity.ERROR:
        return None, None
    if fault is not None:
        return str(fault.code)[:64], describe_error(fault.code)[:500]
    codes = device.errors.active_codes
    if codes:
        code = str(errors.normalise_code(codes[0]) or codes[0])
        return code[:64], describe_error(codes[0])[:500]
    mode = work_mode(device)
    return mode.name.lower()[:64], f"mower reports {mode.name}"[:500]


def raw_of(
    device: MowerDevice, device_name: str, *, link: str | None = None, model: str = ""
) -> dict[str, Any]:
    """A small JSON-safe summary for ``MowerState.raw`` (never secrets, IMEI or ICCID).

    The device's own blade counter (``report_data.maintenance.blade_used_time``, seconds since
    the blade was last reset in the Mammotion app) and its warning threshold are added as
    ``blade_used_s`` and ``blade_used_warn_s`` when the device reported them (> 0); Telemetry
    prefers them over its own blade hours. The device's lifetime battery cycle counter is added
    as ``bat_cycles`` (see :func:`battery_cycles_of`) when reported; Telemetry prefers it over
    its own cycle count. The device's lifetime working time and distance are added as
    ``work_time_s`` and ``mileage_m`` (see :func:`lifetime_totals_of`) when reported; Telemetry
    shows them next to its own mowing hours and distance.
    """
    rd = device.report_data
    rtk = rd.rtk
    raw: dict[str, Any] = {
        "device_name": device_name,
        "work_mode": work_mode(device).name,
        "charge_state": rd.dev.charge_state,
        "rtk_status": RTKStatus.from_value(rtk.status).name,
        "rtk_switch": rtk.mqtt_rtk_info.rtk_switch,
        "satellites": rtk.gps_stars,
        "correction_age_s": rtk.age,
        "progress_percent": rd.work.mow_percent,
        "work_zone": device.location.work_zone,
        "used_net": rd.connect.used_net,
        "mnet_rssi": rd.connect.mnet_rssi,
        "wifi_rssi": rd.connect.wifi_rssi,
    }
    if link is not None:
        raw["link"] = link
    if model:
        raw["model"] = model[:64]
    blade = rd.maintenance.blade_used_time
    if blade.blade_used_time > 0:
        raw["blade_used_s"] = int(blade.blade_used_time)
    if blade.blade_used_warn_time > 0:
        raw["blade_used_warn_s"] = int(blade.blade_used_warn_time)
    cycles = battery_cycles_of(device)
    if cycles is not None:
        raw["bat_cycles"] = cycles
    work_time, mileage = lifetime_totals_of(device)
    if work_time is not None:
        raw["work_time_s"] = work_time
    if mileage is not None:
        raw["mileage_m"] = mileage
    work = rd.work
    if work.cutter_offset > 0:
        raw["cutter_offset_m"] = float(work.cutter_offset)
    if work.cutter_width > 0:
        raw["cutter_width_m"] = float(work.cutter_width)
    raw["pos_type"] = _int(device.location.position_type)
    return raw


def _int(value: object) -> int:
    """A numeric device field as an int, 0 when it holds no finite number.

    PyMammotion copies many protobuf fields into its device model as they were decoded, and a
    corrupt frame whose field arrived with the wrong wire type leaves ``None`` (or bytes) there
    until the next good frame; reading such a field must not stop the state updates."""
    return int(value) if isinstance(value, int | float) and math.isfinite(value) else 0


def _float(value: object) -> float:
    """Like :func:`_int` for a float field (0.0 when it holds no finite number)."""
    return float(value) if isinstance(value, int | float) and math.isfinite(value) else 0.0


_GNSS_SOLUTION: Final[Mapping[RTKStatus, GnssSolution]] = {
    RTKStatus.NONE: GnssSolution.NONE,
    RTKStatus.SINGLE: GnssSolution.SINGLE,
    RTKStatus.FLOAT: GnssSolution.FLOAT,
    RTKStatus.FIX: GnssSolution.FIXED,
}
_PACKED_LOW: Final = 0xFFFF


def job_area_id(device: MowerDevice) -> str | None:
    """The map area id (:func:`map_of` keeps the zone hash as text) of the area the mower works
    in (``location.work_zone``), None when the hash is not one of the map's areas (0, a
    placeholder, or a map that has not arrived yet)."""
    zone = _int(device.location.work_zone)
    return str(zone) if zone and zone in device.map.area else None


def job_of(device: MowerDevice, *, has_report: bool = True) -> JobProgress | None:
    """The running job for ``MowerState.job``, None while no job runs (see
    :func:`path_job_running`: working, paused, returning).

    ``report_data.work`` packs two 16-bit values into each of two fields (the same reading as
    Mammotion-HA's ``sensor.py``): ``area`` holds the progress in percent (high half,
    PyMammotion's ``mow_percent``) and the mowed area in square metres (low half,
    ``area_mowed``); ``progress`` holds the minutes left (high half) and the job's total
    minutes (low half), so elapsed is total minus left. Minutes are given in seconds. A total of
    0 means the mower has not estimated the job yet, then both times are null.
    """
    if not has_report or not path_job_running(device):
        return None
    work = device.report_data.work
    area = _int(work.area)
    packed_time = _int(work.progress)
    total_min = packed_time & _PACKED_LOW
    left_min = packed_time >> 16
    elapsed_s = remaining_s = None
    if total_min > 0:
        remaining_s = min(left_min, total_min) * 60
        elapsed_s = max(0, total_min - left_min) * 60
    return JobProgress(
        progress_percent=max(0, min(100, area >> 16)),
        area_id=job_area_id(device),
        elapsed_s=elapsed_s,
        remaining_s=remaining_s,
        mowed_m2=float(area & _PACKED_LOW),
    )


def gnss_of(device: MowerDevice, *, has_report: bool = True) -> GnssStatus | None:
    """Satellites (``report_data.rtk.gps_stars``), correction age in seconds (``rtk.age``,
    null while it is 0, as without corrections) and the receiver's solution (``rtk.status``,
    PyMammotion's ``RTKStatus``) for ``MowerState.gnss``; None before the first report."""
    if not has_report:
        return None
    rtk = device.report_data.rtk
    age = _float(rtk.age)
    return GnssStatus(
        satellites=max(0, min(255, _int(rtk.gps_stars))),
        correction_age_s=age if age > 0 else None,
        rtk_status=_GNSS_SOLUTION.get(RTKStatus.from_value(_int(rtk.status)), GnssSolution.UNKNOWN),
    )


def _dbm(value: object) -> int | None:
    """A reported signal in dBm, None when it is 0 (not reported) or outside [-150, -1]."""
    dbm = _int(value)
    return dbm if -150 <= dbm <= -1 else None


def network_of(device: MowerDevice) -> NetworkKind:
    """The mower's uplink, the order of PyMammotion's ``device_connection``: ``used_net``
    (``WIFI`` / ``MNET``, also with the ``NET_USED_TYPE_`` prefix; ``TC_MNET`` is mobile), else
    ``connect_type`` (2 Wi-Fi, 1 mobile), else a Bluetooth RSSI (``ble``), else ``none``."""
    connect = device.report_data.connect
    used = str(connect.used_net or "").upper()
    if used.endswith("WIFI"):
        return NetworkKind.WIFI
    if used.endswith("MNET"):
        return NetworkKind.MOBILE
    kind = _int(connect.connect_type)
    if kind == 2:
        return NetworkKind.WIFI
    if kind == 1:
        return NetworkKind.MOBILE
    if _int(connect.ble_rssi) != 0:
        return NetworkKind.BLE
    return NetworkKind.NONE


def signal_of(device: MowerDevice, *, has_report: bool = True) -> SignalStatus | None:
    """Wi-Fi and mobile signal in dBm (``report_data.connect.wifi_rssi`` / ``mnet_rssi``, null
    when 0 or implausible) and the uplink (:func:`network_of`) for ``MowerState.signal``; None
    before the first report."""
    if not has_report:
        return None
    connect = device.report_data.connect
    return SignalStatus(
        wifi_dbm=_dbm(connect.wifi_rssi),
        mobile_dbm=_dbm(connect.mnet_rssi),
        network=network_of(device),
    )


def self_check_of(device: MowerDevice, *, has_report: bool = True) -> SelfCheck | None:
    """The start self-check (``report_data.dev.self_check_status``) for ``MowerState.self_check``:
    ``ok`` for the normal values, else ``blocked`` with :func:`errors.self_check_name` as the
    detail; None before the first report."""
    if not has_report:
        return None
    status = _int(device.report_data.dev.self_check_status)
    if status in errors.SELF_CHECK_NORMAL:
        return SelfCheck(result=SelfCheckResult.OK)
    return SelfCheck(result=SelfCheckResult.BLOCKED, detail=errors.self_check_name(status))


def battery_cycles_of(device: MowerDevice) -> int | None:
    """The battery's lifetime charge cycle counter as the mower reports it, None when unknown.

    PyMammotion fills ``report_data.maintenance.bat_cycles`` from the maintenance block of the
    device report (the same block as the blade counter) and from the ``networkInfo`` property;
    ``mower_state.charge_settings.bat_cycle_times`` (BMS charge settings) is the fallback.
    Zero means "not reported" (models with removable packs never send it).
    """
    cycles = _int(device.report_data.maintenance.bat_cycles)
    if cycles > 0:
        return cycles
    bms = _int(device.mower_state.charge_settings.bat_cycle_times)
    return bms if bms > 0 else None


def lifetime_totals_of(device: MowerDevice) -> tuple[int | None, int | None]:
    """The mower's lifetime working time (seconds) and distance (metres), None when unknown.

    PyMammotion fills ``report_data.maintenance.work_time`` and ``.mileage`` from the
    maintenance block of the device report (the same block as the blade counter, the vendor
    app's "total working time" and "total distance"); ``report_data.dev.work_time_sec`` and
    ``.mileage`` (from the ``deviceOtherInfo`` / ``networkInfo`` properties) are the fallback.
    Zero means "not reported".
    """
    rd = device.report_data
    return (
        _first_positive(rd.maintenance.work_time, rd.dev.work_time_sec),
        _first_positive(rd.maintenance.mileage, rd.dev.mileage),
    )


def _first_positive(*values: object) -> int | None:
    """The first value that holds a positive number, as an int; None when none does."""
    for value in values:
        number = _int(value)
        if number > 0:
            return number
    return None


def blade_height_of(device: MowerDevice) -> int | None:
    """Current cutting height in millimetres (``report_data.work.knife_height``), if plausible."""
    height = _int(device.report_data.work.knife_height)
    return height if 0 < height <= 200 else None


MAX_PLAUSIBLE_SPEED_MPS: Final = 5.0


def speed_of(device: MowerDevice) -> float | None:
    """The working speed in m/s, as the Mammotion app and Mammotion-HA show it.

    Preferred: the reported ``report_data.work.man_run_speed`` (centimetres per second, so
    divided by 100). Fallback while it is 0 or implausible: the configured
    ``mower_state.travel_speed``. None when neither lies in ``(0, 5]`` m/s.
    """
    reported = _float(device.report_data.work.man_run_speed) / 100
    if 0 < reported <= MAX_PLAUSIBLE_SPEED_MPS:
        return float(reported)
    speed = _float(device.mower_state.travel_speed)
    return speed if 0 < speed <= MAX_PLAUSIBLE_SPEED_MPS else None


def state_of(
    device: MowerDevice,
    *,
    mower_id: UUID,
    account_id: UUID,
    connection: ConnectionKind,
    device_name: str,
    firmware: str,
    observed_at: datetime,
    has_report: bool = True,
    link: str | None = None,
    model: str = "",
    light_reported: bool = False,
    speed_setpoint_mps: float | None = None,
    links: Mapping[str, LinkStatus] | None = None,
    firmware_info: FirmwareInfo | None = None,
    fault_since: datetime | None = None,
    warnings: Sequence[FaultWarning] = (),
    settings: Mapping[str, SettingValue] | None = None,
    rain_detected: bool | None = None,
    base_station: BaseStationState | None = None,
) -> MowerState:
    """Build the :class:`MowerState` snapshot of ``device``.

    ``base_station`` is the linked RTK base station (:func:`base_station_of`, ADR 0047).

    ``light_reported``: the mower answered a headlamp read (see :func:`light_on_of`).
    ``speed_setpoint_mps``: the speed the next job will use (the adapter's last ``SetSpeed`` or
    its default); reported as ``speed_mps`` while the mower reports no speed (idle, docked),
    with ``raw["speed_source"]`` ``setpoint`` instead of ``reported``. ``links`` and
    ``firmware_info`` are the connection's additional links and firmware state (ADR 0038).
    ``settings`` and ``rain_detected`` are the mower's own settings and its rain sensor (ADR 0046,
    :mod:`mowdeck_control_mammotion.settings`).

    Faults (:func:`fault_of`): ``fault_since`` is when the adapter saw the mower pause off the
    charger or fail (minus :data:`FAULT_TOLERANCE_S`), ``warnings`` the warning codes it received.
    A pause with a fault logged since then is ``error`` with that code (a trapped mower pauses,
    it never reports an error mode); an error mode prefers it over older list entries. An error
    carries ``raw["error_category"]`` (:class:`~mowdeck_control_mammotion.errors.ErrorCategory`)
    and ``raw["error_source"]``, and ``drive_recoverable`` for the trapped, obstacle, outside
    and wheel slip categories. ``raw["self_check"]`` names what the mower's self-check reports
    as stopping it from starting (when not normal).
    """
    activity = activity_of(device, has_report=has_report)
    fault = None
    if fault_window_open(device, activity):
        fault = fault_of(device, fault_since, warnings)
    if fault is not None:
        activity = MowerActivity.ERROR
    code, message = error_of(device, activity, fault)
    battery = _int(device.report_data.dev.battery_val)
    raw = raw_of(device, device_name, link=link, model=model)
    speed = speed_of(device)
    if speed is not None:
        raw["speed_source"] = "reported"
    elif speed_setpoint_mps is not None and 0 < speed_setpoint_mps <= MAX_PLAUSIBLE_SPEED_MPS:
        speed = float(speed_setpoint_mps)
        raw["speed_source"] = "setpoint"
    light_on = light_on_of(device, reported=light_reported)
    if light_on is not None:
        lamp = device.mower_state.lamp_info
        raw["manual_light"] = bool(lamp.manual_light)
        raw["lamp_bright"] = _int(lamp.lamp_bright)
    category: errors.ErrorCategory | None = None
    if activity is MowerActivity.ERROR and code is not None:
        numeric = errors.normalise_code(code) is not None
        category = errors.category_of(code) if numeric else errors.ErrorCategory.OTHER
        raw["error_category"] = str(category)
        if fault is not None:
            raw["error_source"] = fault.source
        else:
            raw["error_source"] = "error_log" if numeric else "work_mode"
        if category is errors.ErrorCategory.LIFT_TILT:
            raw["lifted"] = True
    self_check = device.report_data.dev.self_check_status
    if self_check not in errors.SELF_CHECK_NORMAL:
        raw["self_check"] = errors.self_check_name(self_check)
    rtk_fix = rtk_fix_of(device, has_report=has_report)
    position, position_source = located_position_of(device, activity, rtk_fix)
    return MowerState(
        mower_id=mower_id,
        account_id=account_id,
        observed_at=observed_at,
        activity=activity,
        battery_percent=max(0, min(100, battery)),
        charging=device.report_data.dev.charge_state != 0 and battery < 100,
        position=position,
        rtk_fix=rtk_fix,
        rtk_source=rtk_source_of(device, has_report=has_report),
        position_source=position_source,
        connection=connection,
        blade_height_mm=blade_height_of(device),
        speed_mps=speed,
        error_code=code,
        error_message=message,
        firmware=firmware[:64] or None,
        light_on=light_on,
        links=dict(links) if links else {},
        firmware_info=firmware_info,
        drive_speed_mps=drive_speed_of(device),
        drive_recoverable=category in errors.RECOVERABLE_CATEGORIES,
        turn_style=turn_style_of(device),
        job=job_of(device, has_report=has_report),
        gnss=gnss_of(device, has_report=has_report),
        signal=signal_of(device, has_report=has_report),
        self_check=self_check_of(device, has_report=has_report),
        error_category=MowerErrorCategory(category) if category is not None else None,
        settings=dict(settings) if settings else {},
        rain_detected=rain_detected,
        base_station=base_station,
        raw=raw,
    )


# --- Links and firmware (ADR 0038) ------------------------------------------------------------


def link_status_of(
    proxy: ProxyInfo | None, *, connected: bool, since: datetime | None, rssi: int | None
) -> LinkStatus:
    """The state of the bridge link for ``MowerState.links``: the link flags plus what the
    bridge said about itself when the link opened (``proxy``, None before the first open)."""
    return LinkStatus(
        connected=connected,
        since=since,
        rssi=rssi if connected and rssi is not None and -128 <= rssi <= 20 else None,
        esphome_version=proxy.esphome_version if proxy else None,
        compiled_at=proxy.compiled_at if proxy else None,
        project_version=proxy.project_version if proxy else None,
        outdated=proxy.outdated if proxy else False,
        outdated_reason=proxy.outdated_reason if proxy else None,
    )


FIRMWARE_UPDATE_MAX_S: Final = 45 * 60.0
"""An update that neither finished nor failed within this time ends as ``unknown``."""


def _clean_version(value: object) -> str | None:
    text = str(value or "").strip()
    return text[:64] if text and text != FIRMWARE_PLACEHOLDER else None


def available_version_of(check: CheckDeviceVersion | None) -> str | None:
    """The version the vendor offers, None when the mower is up to date (or nothing is known)."""
    if check is None or not check.upgradeable or check.product_version_info_vo is None:
        return None
    return _clean_version(check.product_version_info_vo.release_version)


def firmware_info_of(
    check: CheckDeviceVersion | None,
    *,
    installed: str,
    checked_at: datetime | None,
    installing: bool = False,
) -> FirmwareInfo:
    """A :class:`FirmwareInfo` from the vendor's ``CheckDeviceVersion`` (``None``: no check yet,
    ``update_state`` ``unknown``). ``installed`` is the adapter's firmware version, falling
    back to the check's ``current_version``; ``installing`` (or the check's ``isupgrading``)
    reports a running update with the check's ``progress``."""
    available = available_version_of(check)
    running = installing or bool(check is not None and check.isupgrading)
    if running:
        state = "installing"
    elif check is None:
        state = "unknown"
    elif available:
        state = "available"
    else:
        state = "idle"
    notes = (
        check.product_version_info_vo.release_note
        if check is not None and available and check.product_version_info_vo is not None
        else None
    )
    progress = check.progress if check is not None and running else None
    return FirmwareInfo(
        installed=_clean_version(installed)
        or _clean_version(check.current_version if check else None),
        available=available,
        release_notes=str(notes)[:4000] if notes else None,
        update_state=state,  # type: ignore[arg-type]
        progress=progress if progress is not None and 0 <= progress <= 100 else None,
        checked_at=checked_at,
    )


def _live_ota_frame(device: MowerDevice, *, since: float) -> OTAProgress | None:
    """The device's last upgrade report (``events.ota_progress``) when one arrived in this
    process (``ota_progress_at``, PyMammotion's monotonic stamp) and not before ``since``;
    the default frame reads as "complete" and must not count."""
    stamped = float(getattr(device, "ota_progress_at", 0.0) or 0.0)
    if stamped <= 0.0 or stamped < since:
        return None
    frame: OTAProgress | None = getattr(getattr(device, "events", None), "ota_progress", None)
    return frame


@dataclass(slots=True)
class FirmwareTracker:
    """The firmware state of one connection: the last vendor check and the course of an update.

    An update starts with :meth:`started` (or when the device reports ``isupgrading`` out of the
    blue: someone started it in the vendor app) and ends when :meth:`observe` sees a report
    with the target firmware version or a completed OTA frame for it (``idle``; the mower also
    reports each upgraded component as complete, which must not end it), a failed OTA frame
    (``failed`` with the mower's message), or :data:`FIRMWARE_UPDATE_MAX_S` passed
    (``unknown``). Progress comes from ``device.update_check`` (the MQTT property
    ``otaProgress`` and the BLE ``toapp_upgrade_report`` both land there).
    """

    info: FirmwareInfo | None = None
    started_at: float | None = None
    """Loop time when the running update began (None: no update runs)."""
    started_from: str | None = None
    """The installed version when the update began."""
    started_mono: float = 0.0
    """``time.monotonic()`` when MowDeck requested the update: OTA frames stamped before it
    belong to an earlier update and are ignored."""
    check: CheckDeviceVersion | None = None
    last_frame: tuple[str, str, int, int, str] | None = None
    """The last upgrade report that was logged (each distinct report is logged once)."""

    @property
    def installing(self) -> bool:
        return self.started_at is not None

    def checked(
        self, check: CheckDeviceVersion | None, *, installed: str, now: datetime
    ) -> FirmwareInfo:
        """Record a vendor check (a running update keeps its state)."""
        self.check = check
        self.info = firmware_info_of(
            check, installed=installed, checked_at=now, installing=self.installing
        )
        if self.info.update_state == "installing" and not self.installing:
            self.started_at = float("nan")  # started elsewhere; the clock is set on observe
            self.started_from = _clean_version(installed)
        return self.info

    def started(self, version: str, *, installed: str, loop_time: float) -> FirmwareInfo:
        """The vendor accepted the update request."""
        self.started_at = loop_time
        self.started_from = _clean_version(installed)
        self.started_mono = time.monotonic()
        previous = self.info or FirmwareInfo()
        self.info = previous.model_copy(
            update={"available": version, "update_state": "installing", "progress": 0}
        )
        return self.info

    def observe(
        self, device: MowerDevice, *, installed: str, loop_time: float, now: datetime
    ) -> str | None:
        """Fold a device update in; returns ``"finished"``, ``"failed"``, ``"unknown"`` when a
        running update ended (the caller republishes and re-checks), ``"changed"`` when only
        the progress moved, None otherwise."""
        check = device.update_check
        if not self.installing:
            if check.isupgrading:
                self.started_at = loop_time
                self.started_from = _clean_version(installed)
                self.info = firmware_info_of(
                    self.check or check, installed=installed, checked_at=now, installing=True
                )
                self.info = self.info.model_copy(update={"progress": check.progress or 0})
                return "changed"
            return None
        if self.started_at is not None and math.isnan(self.started_at):
            self.started_at = loop_time
        assert self.info is not None  # noqa: S101 - set when the update started
        current = _clean_version(installed)
        ota = _live_ota_frame(device, since=self.started_mono)
        if ota is not None:
            key = (ota.devname, ota.version, ota.progress, ota.result, ota.message)
            if key != self.last_frame:
                self.last_frame = key
                _log.info(
                    "upgrade report: component %s version %s progress %s result %s (%s)",
                    *key,
                )
        if ota is not None and ota.is_failed:
            reason = (ota.message or "the mower reported an error").strip()[:500]
            return self._ended("failed", installed=current, now=now, reason=reason)
        # The mower upgrades several components one after the other and reports each one as
        # complete (``result`` 0) with its own name and version; PyMammotion then also writes
        # that component's version as the device version. So only the target version ends the
        # whole update (any change, when the target is unknown: started in the vendor app).
        target = _clean_version(self.info.available)
        if current is not None and current != self.started_from:
            if target is None or current == target:
                return self._ended("finished", installed=current, now=now)
            current = self.started_from
        if ota is not None and ota.is_complete:
            done_version = _clean_version(ota.version)
            if target is not None and done_version == target:
                return self._ended("finished", installed=done_version, now=now)
        if loop_time - (self.started_at or loop_time) > FIRMWARE_UPDATE_MAX_S:
            return self._ended("unknown", installed=current, now=now)
        progress = (
            check.progress if check.progress is not None and 0 <= check.progress <= 100 else None
        )
        if progress is not None and progress != self.info.progress:
            self.info = self.info.model_copy(update={"progress": progress})
            return "changed"
        return None

    def _ended(
        self, how: str, *, installed: str | None, now: datetime, reason: str | None = None
    ) -> str:
        assert self.info is not None  # noqa: S101 - set when the update started
        self.started_at = None
        self.started_from = None
        self.started_mono = 0.0
        self.check = None
        if how == "finished":
            self.info = self.info.model_copy(
                update={
                    "installed": installed or self.info.available,
                    "available": None,
                    "release_notes": None,
                    "update_state": "idle",
                    "progress": None,
                    "failure_reason": None,
                    "checked_at": now,
                }
            )
        else:
            self.info = self.info.model_copy(
                update={
                    "installed": installed or self.info.installed,
                    "update_state": how,
                    "progress": None,
                    "failure_reason": reason,
                    "checked_at": now,
                }
            )
        return how


# --- Light ------------------------------------------------------------------------------------


def has_light(device_name: str, product_key: str = "") -> bool:
    """True for models with the camera's headlamp ("fill light"): PyMammotion's
    ``DeviceType.is_support_fill_light`` (the app's own gate for the manual and night light
    rows; true for the LUBA 2 AWD X (``Luba-VP…``), the Luba 2 VA/LD/MN/HM and the
    Yuka minis; false for the Luba 1 and the big Yuka). ``product_key`` is unused: the helper
    decides by the device name only. The side LEDs (``read_and_set_sidelight``) are a different
    light and not switched here."""
    del product_key
    return DeviceType.is_support_fill_light(device_name)


def light_reported(device: MowerDevice) -> bool:
    """True when ``mower_state.lamp_info.manual_light`` is set, which only an answer of the mower
    can do. PyMammotion's default ``LampInfo`` reads as "off"; an "off" answer looks the same, so
    the adapter relies on the answered read (``MammotionBase``) for it."""
    return bool(device.mower_state.lamp_info.manual_light)


def light_on_of(device: MowerDevice, *, reported: bool = False) -> bool | None:
    """Whether the headlamp is on, None while the mower has not reported it.

    Mammotion-HA's ``manual_light`` switch (``switch.py``) and PyMammotion's ``mower_api.py``:
    ``mower_state.lamp_info.manual_light``, which PyMammotion's reducer sets from the answer
    ``get_lamp_rsp`` with ``get_ids`` 1126 or 1127 (``lamp_manual_ctrl == manual_power_on``).
    ``MowerState.raw`` carries ``manual_light`` and ``lamp_bright`` once reported.
    """
    if not (reported or light_reported(device)):
        return None
    return bool(device.mower_state.lamp_info.manual_light)


# --- Capabilities -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModelProfile:
    """What a Mammotion model family can do natively (spike 0001, section 3.2)."""

    native_patterns: frozenset[str]
    rtk_sources: frozenset[RtkSource]


_ALL_PATTERNS: Final = frozenset(
    {Pattern.STRIPES, Pattern.DIAGONAL, Pattern.CHECKERBOARD, Pattern.EDGE_ONLY, RANDOM_ANGLE}
)
_LUBA1_PATTERNS: Final = _ALL_PATTERNS - {RANDOM_ANGLE}
"""Luba 1 has no angle modes (``build_route_information`` zeroes ``toward_mode``)."""
_RTK_BOTH: Final = frozenset({RtkSource.BASE_STATION, RtkSource.NETWORK})


def model_profile(device_name: str, product_key: str = "") -> ModelProfile:
    """Native patterns and RTK sources per model family (``DeviceType`` from the name)."""
    if DeviceType.is_luba1(device_name, product_key):
        return ModelProfile(_LUBA1_PATTERNS, frozenset({RtkSource.BASE_STATION}))
    return ModelProfile(_ALL_PATTERNS, _RTK_BOTH)


_CUTTING_WIDTH_MM: Final[Mapping[DeviceType, int]] = {
    DeviceType.LUBA: 400,
    DeviceType.LUBA_2: 400,
    DeviceType.LUBA_VP: 400,
    DeviceType.LUBA_YUKA: 320,
}
"""Cutting width per model family. PyMammotion's model table has no blade or cutting width
(``path_spacing`` is the spacing of the mowing lines, not the swath), so these are the
manufacturer's figures: Luba 1, LUBA 2 AWD (``Luba-VS``) and LUBA 2 AWD X (``Luba-VP``) 400 mm,
Yuka 320 mm. Other families (minis, newer models) stay unknown."""


def reported_cutting_width_mm(device: MowerDevice) -> int | None:
    """The device's own cutting width (``report_data.work.cutter_width``, assumed metres,
    unverified) in millimetres, None until it reported one (> 0)."""
    width = device.report_data.work.cutter_width
    return round(width * 1000) if width > 0 else None


def cutting_width_of(device_name: str, product_key: str = "") -> int | None:
    """Cutting width in millimetres of the model family, None when unknown."""
    return _CUTTING_WIDTH_MM.get(DeviceType.value_of_str(device_name, product_key or ""))


def model_name(device_name: str, product_key: str = "", device: object = None) -> str:
    """Human-readable model, for example ``LUBA 2 AWD 5000X`` once the sub model of ``device``
    is known, else ``Luba VP`` (the device type from the name); see :func:`port.model_of`."""
    return model_of(device, device_name, product_key)[:64]


def is_mower(device_name: str, product_key: str = "") -> bool:
    """False for RTK base stations and pool cleaners."""
    return not (
        DeviceType.is_rtk(device_name, product_key)
        or DeviceType.is_swimming_pool(device_name, product_key)
    )


def is_rtk_base(device_name: str, product_key: str = "") -> bool:
    """True for RTK base stations: PyMammotion's ``DeviceType.is_rtk`` (names ``RTK…``,
    ``RBS03A0/1/2…``, ``NB…`` and the RTK product keys)."""
    return bool(DeviceType.is_rtk(device_name, product_key))


def device_type_of(device_name: str, product_key: str = "", *, mower: bool) -> DiscoveredType:
    """What a discovered device is (ADR 0047): ``mower`` when ``mower`` (the caller checked the
    device model and :func:`is_mower`), ``rtk_base`` for :func:`is_rtk_base`, else ``other``."""
    if mower:
        return DiscoveredType.MOWER
    if is_rtk_base(device_name, product_key):
        return DiscoveredType.RTK_BASE
    return DiscoveredType.OTHER


# --- RTK base station (ADR 0047) --------------------------------------------------------------

_CORRECTION_LINK: Final[Mapping[str, RtkCorrectionLink]] = {
    "RTK_USED_LORA": RtkCorrectionLink.LORA,
    "RTK_USED_INTERNET": RtkCorrectionLink.INTERNET,
    "RTK_USED_NRTK": RtkCorrectionLink.NETWORK,
}
"""``report_data.rtk.mqtt_rtk_info.rtk_switch`` (the proto enum name) to the correction link."""


@dataclass(frozen=True, slots=True)
class BaseDetails:
    """What the mower answered to ``basestation_info`` (``ResponseBasestationInfoT``), kept by
    the adapter: the next report replaces ``report_data.basestation_info`` and with it these
    fields."""

    satellites: int | None
    rtk_status: GnssSolution
    wifi_dbm: int | None
    lora_channel: int | None
    at: datetime


def base_details_of(reply: object, device: MowerDevice, *, now: datetime) -> BaseDetails | None:
    """The details of a ``basestation_info`` answer: from the reply's ``base.to_app`` when it
    carries one, else from ``report_data.basestation_info`` as PyMammotion's reducer stored it
    (``_update_base_data``). None when neither holds a value (no answer)."""
    source: object = getattr(getattr(reply, "base", None), "to_app", None)
    if source is None:
        source = device.report_data.basestation_info
    satellites = _int(getattr(source, "sats_num", 0))
    status = _int(getattr(source, "rtk_status", 0))
    wifi = _dbm(getattr(source, "wifi_rssi", 0))
    channel = _int(getattr(source, "lora_channel", 0))
    if not (satellites or status or wifi is not None or channel):
        return None
    return BaseDetails(
        satellites=min(255, satellites) if satellites > 0 else None,
        rtk_status=_GNSS_SOLUTION.get(RTKStatus.from_value(status), GnssSolution.UNKNOWN),
        wifi_dbm=wifi,
        lora_channel=channel if 0 < channel <= 255 else None,
        at=now,
    )


def base_position_of(device: MowerDevice) -> BaseStationPosition | None:
    """Where the base station stands: ``rtk.mqtt_rtk_info.latitude``/``longitude`` (degrees;
    PyMammotion converts them with ``math.radians`` for its RTK origin fallback). ``0, 0`` means
    not known, ``-1, -1`` no GGA from the base station: both None."""
    info = device.report_data.rtk.mqtt_rtk_info
    lat, lon = _float(info.latitude), _float(info.longitude)
    if (lat, lon) in ((0.0, 0.0), (-1.0, -1.0)):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return BaseStationPosition(lat=lat, lon=lon)


def base_firmware_of(device: MowerDevice) -> str | None:
    """The base station's version as the mower reports it (``rpt_basestation_info``
    ``ver_major.ver_minor.ver_patch.ver_build``); None while all parts are 0."""
    info = device.report_data.basestation_info
    parts = [_int(info.ver_major), _int(info.ver_minor), _int(info.ver_patch), _int(info.ver_build)]
    if not any(parts):
        return None
    return ".".join(str(p) for p in parts)[:64]


def base_station_of(
    device: MowerDevice,
    *,
    ref: BaseStationRef,
    has_report: bool = True,
    details: BaseDetails | None = None,
    firmware_info: FirmwareInfo | None = None,
) -> BaseStationState:
    """The linked base station for ``MowerState.base_station`` from the mower's own view.

    From every report (``report_data.rtk``): the correction link (``mqtt_rtk_info.rtk_switch``:
    LoRa, internet or network RTK), over LoRa the radio link (``lora_info``
    ``lora_connection_status > 0``) and its channel (``pair_code_channel``), the base position
    (:func:`base_position_of`) and whether it was moved (``score_info.base_moved`` or
    ``base_moving``). The version from ``rpt_basestation_info``; satellites, solution and Wi-Fi
    from the last ``basestation_info`` answer (``details``); the firmware state from the vendor.
    Before the first report only the reference, the details and the firmware are known.
    """
    base = BaseStationState(
        base_station_id=ref.base_station_id,
        firmware_info=firmware_info,
        satellites=details.satellites if details else None,
        rtk_status=details.rtk_status if details else GnssSolution.UNKNOWN,
        wifi_dbm=details.wifi_dbm if details else None,
        lora_channel=details.lora_channel if details else None,
        details_at=details.at if details else None,
    )
    if not has_report:
        return base
    rtk = device.report_data.rtk
    link = _CORRECTION_LINK.get(str(rtk.mqtt_rtk_info.rtk_switch), RtkCorrectionLink.UNKNOWN)
    lora = rtk.lora_info
    channel = _int(lora.pair_code_channel)
    score = rtk.score_info
    return base.model_copy(
        update={
            "correction_link": link,
            "link_up": _int(lora.lora_connection_status) > 0
            if link is RtkCorrectionLink.LORA
            else None,
            "lora_channel": base.lora_channel or (channel if 0 < channel <= 255 else None),
            "position": base_position_of(device),
            "moved": bool(_int(score.base_moved) or _int(score.base_moving)),
            "firmware": base_firmware_of(device),
        }
    )


def _number(config: Mapping[str, Any], key: str) -> float | None:
    value = config.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _range(config: Mapping[str, Any], low_key: str, high_key: str) -> tuple[float, float] | None:
    low, high = _number(config, low_key), _number(config, high_key)
    if low is None or high is None or not 0 < low <= high:
        return None
    return low, high


def model_limits(
    device: MowerDevice, product_key: str = ""
) -> tuple[tuple[int, int] | None, tuple[float, float] | None]:
    """``(blade height range in mm, mowing speed range in m/s)`` of the model, each None when
    the model or the value is unknown. Speeds above ``MAX_PLAUSIBLE_SPEED_MPS`` are dropped."""
    config = model_config_of(device, product_key)
    if config is None:
        return None, None
    blade_mm = _range(config, "blade_height_min", "blade_height_max")
    blade = (int(blade_mm[0]), int(blade_mm[1])) if blade_mm else None
    speed = _range(config, "cutting_speed_min", "cutting_speed_max") or _range(
        config, "working_speed_min", "working_speed_max"
    )
    if speed is not None and speed[1] > MAX_PLAUSIBLE_SPEED_MPS:
        speed = None
    return blade, speed


def drive_speed_setting_of(device: MowerDevice, product_key: str = "") -> DriveSpeedSetting:
    """The range of the speed setting for manual driving (see
    :data:`DRIVE_SPEED_DEFAULT_RANGE_MPS`): the model's travel speed limits, else its speed
    range (:func:`model_limits`), else the default."""
    config = model_config_of(device, product_key)
    speed = _range(config, "working_speed_min", "working_speed_max") if config else None
    if speed is None or speed[1] > MAX_PLAUSIBLE_SPEED_MPS:
        speed = model_limits(device, product_key)[1] or DRIVE_SPEED_DEFAULT_RANGE_MPS
    return DriveSpeedSetting(min_mps=speed[0], max_mps=speed[1], step_mps=DRIVE_SPEED_STEP_MPS)


def drive_speed_of(device: MowerDevice) -> float | None:
    """The speed setting for manual driving the mower reported last
    (``mower_state.travel_speed``, the answer to ``get_speed``); None while it is 0 or
    implausible."""
    speed = _float(device.mower_state.travel_speed)
    return speed if 0 < speed <= MAX_PLAUSIBLE_SPEED_MPS else None


TURNING_MODES: Final[dict[int, TurnStyle]] = {0: TurnStyle.IN_PLACE, 1: TurnStyle.MULTI_POINT}
"""The mower's own turn setting for its jobs (ADR 0043): ``mower_state.turning_mode``, which
PyMammotion's state reducer sets from the read/write parameter 6 (``nav_sys_param_cmd`` on a
Luba Pro, ``bidire_comm_cmd`` otherwise; ``state_reducer.py``: "0=zero-turn, 1=multipoint
turn"). Since ADR 0046 it is also the setting ``turn_style``
(:mod:`mowdeck_control_mammotion.settings`), which reads and changes it."""


def turn_style_of(device: MowerDevice) -> TurnStyle | None:
    """The mower's own turn style (:data:`TURNING_MODES`); None for an unknown value.

    PyMammotion starts at 0, so until the mower answers the settings read (parameter 6) the
    value reads as zero-turn (``in_place``)."""
    return TURNING_MODES.get(_int(device.mower_state.turning_mode))


def path_spacing_of(device: MowerDevice, product_key: str = "") -> tuple[int, int] | None:
    """Path spacing range in centimetres of the model's table entry, None when unknown."""
    config = model_config_of(device, product_key)
    spacing = _range(config, "path_spacing_min", "path_spacing_max") if config else None
    return (int(spacing[0]), int(spacing[1])) if spacing else None


def maintenance_actions_of(
    device_name: str, product_key: str = "", *, undock: bool = False
) -> frozenset[MaintenanceAction]:
    """The maintenance commands of a model (ADR 0048), as Mammotion-HA offers its buttons
    (``button.py``): map sync, RTK dock resync, release from dock and charging station
    relocation for every model, the remote restart for every model but the Luba 1
    (``BUTTON_LUBA_PRO_YUKA``). ``leave_dock`` only with ``undock``.
    """
    actions: set[MaintenanceAction] = {"sync_map", "relocate_dock", "resync_rtk"}
    if not DeviceType.is_luba1(device_name, product_key):
        actions.add("restart_mower")
    if undock:
        actions.add("leave_dock")
    return frozenset(actions)


_START_OPTION_FAMILIES: Final = frozenset({DeviceType.LUBA_2, DeviceType.LUBA_VP})
"""Model families whose job options were checked against the Mammotion app: LUBA 2 AWD
(``Luba-VS``) and LUBA 2 AWD X (``Luba-VP``), whose "task assignment" offers obstacle
detection, laps around no-go zones, path order and start progress."""

OBSTACLE_LAPS_RANGE: Final = (0, 4)
"""``ObstacleLapsMode`` in PyMammotion: none to four laps around each no-go zone."""
START_PROGRESS_RANGE_PCT: Final = (0, 99)
"""``start_progress`` in percent; Mammotion's cloud schema caps it at 99 (Mammotion-HA)."""
DEFAULT_OBSTACLE_LAPS: Final = 1
"""``obstacle_laps`` sent when a start sets none (PyMammotion's ``OperationSettings`` default,
what this adapter always sent)."""
_PATH_ORDER: Final[Mapping[PathOrder, MowOrder]] = {
    PathOrder.BORDER_FIRST: MowOrder.border_first,
    PathOrder.GRID_FIRST: MowOrder.grid_first,
}


def detection_strategies_of(
    device_name: str, firmware: str = "", product_key: str = ""
) -> list[DetectionStrategy]:
    """The obstacle detection modes the app offers on this model and firmware, in its order
    (``DetectionStrategy.for_device``: LUBA 2 AWD below firmware 1.12.0 has the old touch
    list, newer firmware and the AWD X have off / standard / sensitive); empty outside
    :data:`_START_OPTION_FAMILIES`."""
    if DeviceType.value_of_str(device_name, product_key or "") not in _START_OPTION_FAMILIES:
        return []
    return DetectionStrategy.for_device(device_name, firmware)


def start_options_of(device_name: str, firmware: str = "", product_key: str = "") -> StartOptions:
    """The job options of a model (``MowerCapabilities.start_options``, ADR 0030).

    LUBA 2 AWD and AWD X: obstacle detection with the app's modes for the firmware (named by
    PyMammotion's option keys: ``off``, ``slow_touch``, ``less_touch``, ``standard``,
    ``sensitive``), zero to four laps around no-go zones, both path orders and a start progress
    of 0 to 99 %. Other models keep only the laps around no-go zones, which this adapter
    already took as a pattern extra (``obstacle_laps``) for every model.
    """
    strategies = detection_strategies_of(device_name, firmware, product_key)
    if not strategies:
        return StartOptions(obstacle_laps_range=OBSTACLE_LAPS_RANGE)
    return StartOptions(
        obstacle_detection=[
            ObstacleDetection(strategy.option_key(strategies)) for strategy in strategies
        ],
        obstacle_laps_range=OBSTACLE_LAPS_RANGE,
        orders=[PathOrder.BORDER_FIRST, PathOrder.GRID_FIRST],
        start_progress_range_pct=START_PROGRESS_RANGE_PCT,
    )


def capabilities_of(
    device: MowerDevice,
    *,
    device_name: str,
    product_key: str,
    manual_drive: bool,
    manual_drive_blades: bool,
    drive_reverse: bool | None = None,
    camera: bool = False,
    model: str = "",
    links_supported: Sequence[str] = (),
    firmware_update: bool = False,
    undock: bool = False,
    drive_speed: bool = False,
    settings: Sequence[SettingSpec] = (),
    maintenance: bool = False,
    firmware: str = "",
) -> MowerCapabilities:
    """Capabilities of one mower over one link (``links_supported`` and ``firmware_update``
    describe the connection, ADR 0038).

    Blade height and speed limits come from PyMammotion's per-model table
    (:func:`port.model_config_of`): the entry of ``mower_state.sub_model_id`` (the exact model,
    sent in answer to ``get_device_product_model``), else of the product key. The speed range is
    the mowing speed (``cutting_speed_min/max``; ``working_speed_min/max`` is the faster travel
    speed and only used when the entry has no cutting speed); the path spacing range comes from
    the same entry (:func:`path_spacing_of`, centimetres there). Without an entry the limits stay
    unset (the controller then rejects the commands): PyMammotion's own fallback for a Luba VP
    is the Yuka table, which is wrong for it. ``drive_speed``: the connection can send driver
    commands, so it declares the speed setting for manual driving
    (:func:`drive_speed_setting_of`). ``drive_reverse`` is the drive link's (ADR 0043, ADR
    0049): a Bluetooth session (``send_movement``) takes a negative linear speed, the cloud
    remote drive does not; None follows ``manual_drive`` (a Bluetooth link). ``maintenance``:
    the connection can send the maintenance commands
    (:func:`maintenance_actions_of`, ADR 0048). ``firmware`` picks the obstacle detection
    modes of the job options (:func:`start_options_of`).
    """
    profile = model_profile(device_name, product_key)
    with_camera = camera and has_camera(device_name, product_key)
    blade, speed = model_limits(device, product_key)
    spacing = path_spacing_of(device, product_key)
    return MowerCapabilities(
        light=has_light(device_name, product_key),
        model=model[:64] if model else model_name(device_name, product_key, device),
        native_patterns=profile.native_patterns,
        manual_drive=manual_drive,
        manual_drive_blades=manual_drive and manual_drive_blades,
        custom_paths=False,
        camera=CameraAccess.CLOUD if with_camera else CameraAccess.NONE,
        rtk_sources=profile.rtk_sources,
        blade_height_range_mm=blade,
        path_spacing_range_mm=(spacing[0] * 10, spacing[1] * 10) if spacing else None,
        cutting_width_mm=reported_cutting_width_mm(device)
        or cutting_width_of(device_name, product_key),
        min_speed_mps=speed[0] if speed else None,
        max_speed_mps=speed[1] if speed else None,
        connection_kinds=CONNECTION_KINDS,
        links_supported=list(links_supported),
        firmware_update=firmware_update,
        undock=undock,
        maintenance=(
            maintenance_actions_of(device_name, product_key, undock=undock)
            if maintenance
            else frozenset()
        ),
        drive_reverse=manual_drive and (manual_drive if drive_reverse is None else drive_reverse),
        drive_speed_setting=drive_speed_setting_of(device, product_key) if drive_speed else None,
        settings=list(settings),
        start_options=start_options_of(device_name, firmware, product_key),
    )


# --- Camera -----------------------------------------------------------------------------------

AGORA_ENCRYPTION_MODE: Final = "aes-256-gcm2"
"""The mode of an encrypted Mammotion channel (``openEncrypt`` with ``key`` and ``salt``)."""
DEFAULT_CAMERA_SESSION_S: Final = 600
"""Assumed lifetime when the token's claims cannot be read."""
MAX_CAMERA_SESSION_S: Final = 24 * 3600
_CAMERA_NAMES: Final[Mapping[int, str]] = {0: "Front left", 1: "Front right", 2: "Rear"}
_CAMERA_PLACEMENTS: Final[Mapping[int, CameraPlacement]] = {
    0: CameraPlacement.FRONT_LEFT,
    1: CameraPlacement.FRONT_RIGHT,
    2: CameraPlacement.REAR,
}


def has_camera(device_name: str, product_key: str = "") -> bool:
    """True for models with a camera: every Mammotion mower except the Luba 1."""
    return is_mower(device_name, product_key) and not DeviceType.is_luba1(device_name, product_key)


def _token_expiry(token: str, now: datetime) -> datetime:
    """When the viewer token ends: issue time plus the shortest expiry of the token and its
    privileges (AccessToken2 claims; the token text itself is never logged)."""
    try:
        claims = decode_agora_token(token)
    except ValueError:
        return now + timedelta(seconds=DEFAULT_CAMERA_SESSION_S)
    lifetimes = [claims.expires_in, *(v for v in claims.privileges.values() if v > 0)]
    seconds = min(v for v in lifetimes if v > 0) if any(v > 0 for v in lifetimes) else 0
    if claims.issued_at <= 0 or seconds <= 0:
        return now + timedelta(seconds=DEFAULT_CAMERA_SESSION_S)
    expires = datetime.fromtimestamp(claims.issued_at + seconds, tz=now.tzinfo)
    if expires <= now:
        # A skewed clock or an odd token: let the client try and renew early.
        return now + timedelta(seconds=60)
    return min(expires, now + timedelta(seconds=MAX_CAMERA_SESSION_S))


def camera_quota_of(video: VideoResourceResponse | None) -> CameraQuota | None:
    """The 4G video budget. ``totalTime`` and ``availableTime`` are read as seconds (as
    Mammotion-HA treats ``availableTime``); unverified on hardware."""
    if video is None or video.totalTime <= 0:
        return None
    total = video.totalTime / 60
    used = max(video.totalTime - max(video.availableTime, 0), 0) / 60
    return CameraQuota(used_minutes=round(used, 1), total_minutes=round(total, 1))


def camera_session_of(
    subscription: StreamSubscriptionResponse,
    *,
    now: datetime,
    video: VideoResourceResponse | None = None,
) -> CameraSession:
    """Map PyMammotion's stream subscription to a :class:`CameraSession`.

    The viewer joins with ``uid`` and ``token``; the mower publishes camera slot ``n`` under
    Agora uid ``n + 1`` (PyMammotion ``MammotionHTTP.get_stream_subscription``), so a camera's
    ``uid`` is ``cameraId + 1`` when ``cameraId`` is a slot index (0 to 2). Slots 0 and 1 are
    the front left and front right camera (``placement``; a stereo pair for the web app).
    ``key`` is used verbatim and ``salt`` stays base64; the browser decodes it.
    """
    listed = [camera.cameraId for camera in subscription.cameras[:8]]
    # The owner's LUBA 2 AWD X answers with cameraId 1, 2, 3 (one-based) while PyMammotion's
    # comment and the simulator use 0, 1, 2: a list of two or more ids that starts at 1, has no 0
    # and stays within 1..3 is read as one-based: slot = cameraId - 1, the Agora uid stays the id.
    one_based = (
        len(listed) >= 2
        and 1 in listed
        and 0 not in listed
        and all(1 <= cid <= 3 for cid in listed)
    )
    cameras = []
    for index, camera in enumerate(subscription.cameras[:8]):
        slot = camera.cameraId - 1 if one_based else camera.cameraId
        name = _CAMERA_NAMES.get(slot, f"Camera {index + 1}")
        uid = slot + 1 if 0 <= slot <= 2 else None
        cameras.append(
            CameraInfo(id=str(slot), name=name, uid=uid, placement=_CAMERA_PLACEMENTS.get(slot))
        )
    if one_based:
        _log.info("camera ids %s read as one-based slots", listed)
    encryption = None
    if subscription.openEncrypt and subscription.key:
        encryption = CameraEncryption(
            mode=AGORA_ENCRYPTION_MODE, key=subscription.key, salt=subscription.salt or None
        )
    return CameraSession(
        provider=CameraProvider.AGORA,
        app_id=subscription.appid,
        channel=subscription.channelName,
        token=subscription.token,
        uid=subscription.uid,
        cameras=cameras,
        encryption=encryption,
        expires_at=_token_expiry(subscription.token, now),
        quota=camera_quota_of(video),
    )


# --- Map --------------------------------------------------------------------------------------


def _frame_points(frames: FrameList) -> list[LocalPoint] | None:
    """All points of a complete frame list in frame order, or None while frames are missing."""
    if not frames.data or len(frames.data) != frames.total_frame:
        return None
    ordered = sorted(frames.data, key=lambda frame: frame.current_frame)
    return [(float(p.x), float(p.y)) for frame in ordered for p in frame.data_couple]


def _names(device: MowerDevice) -> dict[int, str]:
    return {item.hash: item.name for item in device.map.area_name if item.name}


def _zones(groups: Iterable[Mapping[int, FrameList]], names: Mapping[int, str]) -> list[MapZone]:
    zones: list[MapZone] = []
    for group in groups:
        for zone_hash, frames in sorted(group.items()):
            points = _frame_points(frames)
            if points is None or len(points) < 3:
                continue
            name = names.get(zone_hash) or frames.name or None
            zones.append(MapZone(id=str(zone_hash), name=name, boundary=points))
    return zones


def map_complete(device: MowerDevice) -> bool:
    """True when the map has areas and every area's frames arrived."""
    areas = device.map.area
    return bool(areas) and all(_frame_points(frames) is not None for frames in areas.values())


def map_revision(device: MowerDevice) -> str | None:
    """The device's map hash (``report_data.locations[0].bol_hash``), if reported."""
    locations = device.report_data.locations
    if locations and locations[0].bol_hash:
        return str(locations[0].bol_hash)
    return None


def has_vendor_map(device: MowerDevice) -> bool:
    """True when the mower reports a map (``locations[0].bol_hash`` above 1, the vendor app's
    gate for the cloud remote drive) or the adapter holds a complete one."""
    locations = device.report_data.locations
    if locations and (locations[0].bol_hash or 0) > 1:
        return True
    return map_complete(device)


def map_of(device: MowerDevice) -> MowerMap:
    """Convert ``device.map`` (``HashList``) to a :class:`MowerMap` in local metres.

    Areas keep the zone hash as id (``StartMow.area_ids`` are these hashes as strings).
    No-go zones are ``no_go_zone`` plus ``no_go_zone_variant``; obstacles are ``obstacle`` plus
    the vision obstacle zones; virtual walls (lines) and dump zones are not represented.
    Without a reported RTK reference the geo reference is ``(0, 0)``: local geometry stays
    valid for mowing, but WGS84 display is wrong until the mower reports its reference.
    """
    hash_list = device.map
    names = _names(device)
    areas: list[MapArea] = []
    for zone_hash, frames in sorted(hash_list.area.items()):
        points = _frame_points(frames)
        if points is None or len(points) < 3:
            continue
        name = names.get(zone_hash) or frames.name or None
        areas.append(MapArea(id=str(zone_hash), name=name, boundary=points))
    paths: list[MapPath] = []
    for path_hash, frames in sorted(hash_list.path.items()):
        points = _frame_points(frames)
        if points is None or len(points) < 2:
            continue
        paths.append(MapPath(id=str(path_hash), name=frames.name or None, points=points))
    reference = geo_reference_of(device) or GeoReference(origin_lat=0.0, origin_lon=0.0)
    return MowerMap(
        reference=reference,
        areas=areas,
        no_go_zones=_zones((hash_list.no_go_zone, hash_list.no_go_zone_variant), names),
        obstacles=_zones((hash_list.obstacle, hash_list.visual_obstacle_zone), names),
        paths=paths,
        dock=dock_of(device),
        revision=map_revision(device),
    )


def dock_of(device: MowerDevice) -> DockPose | None:
    """The charging station in the local frame.

    PyMammotion stores the dock as ``longitude = x`` and ``latitude = y`` in metres
    (``MowerDevice.buffer``, "latitude Y longitude X") and ``rotation`` as a compass course, the
    same convention as ``location.orientation`` (304 on the owner's mower, the direction the mower
    leaves the dock); the pose carries it as the local frame angle the contract asks for.
    """
    dock = device.location.dock
    if dock.latitude == 0.0 and dock.longitude == 0.0:
        return None
    reference = geo_reference_of(device)
    rotation = reference.rotation_deg if reference is not None else 0.0
    heading = (90.0 - float(dock.rotation) - rotation) % 360.0
    return DockPose(x=float(dock.longitude), y=float(dock.latitude), heading_deg=heading)


def area_summary(mower_map: MowerMap) -> list[dict[str, Any]]:
    """Name, id, size (square metres) and a WGS84 centre of every area, for scripts and logs."""
    summary = []
    for area in mower_map.areas:
        pts = area.boundary
        size = 0.5 * abs(
            sum(
                pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1]
                for i in range(len(pts))
            )
        )
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        lat, lon = to_wgs84(mower_map, cx, cy)
        summary.append(
            {
                "id": area.id,
                "name": area.name,
                "size_m2": round(size, 1),
                "centre": [round(lat, 7), round(lon, 7)],
            }
        )
    return summary


# --- Mow path ---------------------------------------------------------------------------------

_PATH_KINDS: Final[Mapping[int, MowPathKind]] = {0: "mow", 2: "edge"}
"""``cover_path_packet_t.path_type``: 0 the fill stripes, 2 the border laps (PyMammotion's
``generate_geojson.py``, ``MOW_STRIPE_STYLE`` and ``BORDER_PASS_STYLE``); others are unknown."""
_PATH_JOB_MODES: Final = frozenset(
    int(m)
    for m in (
        WorkMode.MODE_WORKING,
        WorkMode.MODE_PAUSE,
        WorkMode.MODE_RETURNING,
        WorkMode.MODE_CHARGING_PAUSE,
        WorkMode.MODE_CORRIDOR_PAUSE,
    )
)
"""Work modes in which a job (and so its route) is in progress, returning included."""


@dataclass(frozen=True, slots=True)
class PlannedPath:
    """A :class:`MowPath` plus the vendor's line hash of every polyline (same order), which the
    progress of the report refers to."""

    path: MowPath
    line_hashes: tuple[int, ...]


def path_job_running(device: MowerDevice) -> bool:
    """True while the work mode is one of a job in progress (mowing, paused, returning)."""
    return device.report_data.dev.sys_status in _PATH_JOB_MODES


def mow_path_of(
    device: MowerDevice, *, planned_at: datetime, complete_only: bool = True
) -> PlannedPath | None:
    """The route PyMammotion's ``MowPathSaga`` collected, in local metres (the map's frame).

    The route is the line hash list (``HashList.line_root_hashlist``, sub_cmd 3, in driving
    order) and per line its cover path packets in ``HashList.current_mow_path``
    (``[transaction][frame].path_packets``, ordered by ``path_cur``), exactly as PyMammotion's
    ``generate_mow_progress_geojson`` assembles them. Transactions with missing frames are
    skipped, packets of lines outside the current list (an older route) are ignored. With
    ``complete_only`` a route with a missing line is None (the progress would be off);
    otherwise the lines that arrived are returned. The id is ``HashList.computed_path_hash``
    (equal to the report's ``work.path_hash`` when the list is current), as text.
    """
    hash_list = device.map
    lines = list(dict.fromkeys(h for h in hash_list.line_root_hashlist if h))
    if not lines:
        return None
    wanted = set(lines)
    incomplete = hash_list.find_missing_mow_path_frames()
    packets: dict[int, dict[int, MowPathPacket]] = {}
    for transaction_id in sorted(hash_list.current_mow_path):
        if transaction_id in incomplete:
            continue
        frames = hash_list.current_mow_path[transaction_id]
        for frame_index in sorted(frames):
            for packet in frames[frame_index].path_packets:
                if packet.path_hash in wanted:
                    packets.setdefault(packet.path_hash, {})[packet.path_cur] = packet
    polylines: list[list[LocalPoint]] = []
    kinds: list[MowPathKind] = []
    zones: list[str] = []
    hashes: list[int] = []
    for line in lines:
        by_cur = packets.get(line)
        points = (
            [(float(p.x), float(p.y)) for cur in sorted(by_cur) for p in by_cur[cur].data_couple]
            if by_cur
            else []
        )
        if not points:
            if complete_only:
                return None
            continue
        first = by_cur[min(by_cur)] if by_cur else None
        polylines.append(points)
        kinds.append(_PATH_KINDS.get(first.path_type, "other") if first else "other")
        if first is not None and first.zone_hash and str(first.zone_hash) not in zones:
            zones.append(str(first.zone_hash))
        hashes.append(line)
    if not polylines:
        return None
    path = MowPath(
        path_hash=str(hash_list.computed_path_hash),
        polylines=polylines,
        kinds=kinds,
        points_total=sum(len(line) for line in polylines),
        area_ids=zones,
        planned_at=planned_at,
    )
    return PlannedPath(path=path, line_hashes=tuple(hashes))


def path_progress_of(device: MowerDevice, planned: PlannedPath) -> MowPathProgress | None:
    """Where the running job is on ``planned``, from the report's ``work``; None when the report
    belongs to another route or names a line the path does not hold.

    ``ub_path_hash`` is the line being driven and ``now_index`` (``real_path_num`` bits 8 to 23)
    the point index within it; the lines before it in the route are done (PyMammotion's
    ``generate_mow_progress_geojson``). The progress counts points over all polylines.
    """
    work = device.report_data.work
    if not path_job_running(device) or str(work.path_hash) != planned.path.path_hash:
        return None
    lines = planned.line_hashes
    if work.ub_path_hash in lines:
        active = lines.index(work.ub_path_hash)
    elif work.ub_path_hash == 0 and len(lines) == 1:
        active = 0
    else:
        return None
    polylines = planned.path.polylines
    done = sum(len(line) for line in polylines[:active])
    done += min(max(work.now_index, 0), len(polylines[active]))
    return MowPathProgress(index=done, direction=work.path_direction & 0xFF)


# --- Commands ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RoutePlan:
    """Everything the StartMow sequence sends, in PyMammotion terms."""

    zone_hashes: tuple[int, ...]
    settings: OperationSettings
    job: NativeJob


def _angle_mode(settings: Mapping[str, Any], pattern: str) -> PathAngleSetting:
    if pattern == RANDOM_ANGLE:
        return PathAngleSetting.random_angle
    mode = str(settings.get("angle_mode", "absolute"))
    try:
        return {
            "absolute": PathAngleSetting.absolute_angle,
            "relative": PathAngleSetting.relative_angle,
            "random": PathAngleSetting.random_angle,
        }[mode]
    except KeyError:
        msg = f"angle_mode must be absolute, relative or random, not {mode!r}"
        raise ValueError(msg) from None


def route_plan(
    command: StartMow,
    capabilities: MowerCapabilities,
    *,
    blade_height_mm: int,
    speed_mps: float,
    path_spacing_cm: tuple[int, int] | None = None,
    detection: Sequence[DetectionStrategy] = (),
) -> RoutePlan:
    """Map ``StartMow`` (area ids and ``pattern`` settings) to PyMammotion route settings.

    ``pattern`` keys: ``name`` (stripes, diagonal, checkerboard, edge_only, random_angle),
    ``angle_deg``, ``spacing_m``, ``border_laps`` and the job options ``obstacle_detection``,
    ``obstacle_laps``, ``order`` and ``start_progress_pct`` (as in
    :meth:`MowRequest.from_start_mow`, checked against ``capabilities.start_options``), plus
    Mammotion extras ``angle_mode`` (absolute, relative, random), ``blade_height_mm`` and
    ``speed_mps``.

    The job options go where Mammotion-HA puts them: ``obstacle_detection`` becomes
    ``ultra_wave`` (the value of the app's option in ``detection``, the model's
    :func:`detection_strategies_of`), ``obstacle_laps`` and ``start_progress`` bytes 1 and 3 of
    the route's ``path_order`` buffer, ``order`` its byte 0 (``border_mode``: 0 border first,
    1 grid first). An option the start leaves out keeps what this adapter sent before:
    PyMammotion's ``ultra_wave`` default, :data:`DEFAULT_OBSTACLE_LAPS`, progress 0, and
    ``border_mode`` equal to the border laps (the adapter's value since the first release;
    whether the firmware reads byte 0 as the order or as laps is not verified on a mower).
    Raises ``ValueError`` (or ``UnsupportedPattern``) for invalid settings.
    """
    request = MowRequest.from_start_mow(command)
    job = plan_to_native_job(request, capabilities)
    settings: Mapping[str, Any] = command.pattern or {}
    try:
        zone_hashes = tuple(int(area_id) for area_id in job.area_ids)
    except ValueError:
        msg = "Mammotion area ids are zone hashes (integers)"
        raise ValueError(msg) from None
    spacing_cm = round(job.spacing_m * 100)
    if path_spacing_cm is not None and not path_spacing_cm[0] <= spacing_cm <= path_spacing_cm[1]:
        low, high = path_spacing_cm
        msg = f"spacing_m must be between {low / 100} and {high / 100} on this mower"
        raise ValueError(msg)
    blade = int(settings.get("blade_height_mm", blade_height_mm))
    speed = float(settings.get("speed_mps", speed_mps))
    if not (math.isfinite(speed) and speed > 0):
        msg = "speed_mps must be positive"
        raise ValueError(msg)
    if job.pattern == Pattern.EDGE_ONLY:
        channel_mode = CuttingMode.no_grid
    elif job.pattern == Pattern.CHECKERBOARD:
        channel_mode = CuttingMode.double_grid
    else:
        channel_mode = CuttingMode.single_grid
    toward = round(job.passes[0]) % 180 if job.passes else 0
    operation = OperationSettings(
        speed=speed,
        channel_mode=int(channel_mode),
        channel_width=spacing_cm,
        blade_height=blade,
        toward=toward,
        toward_included_angle=90,
        toward_mode=int(_angle_mode(settings, job.pattern)),
        border_mode=int(_PATH_ORDER[job.order]) if job.order is not None else job.border_laps,
        mowing_laps=job.border_laps,
        obstacle_laps=DEFAULT_OBSTACLE_LAPS if job.obstacle_laps is None else job.obstacle_laps,
        start_progress=job.start_progress_pct or 0,
        areas=list(zone_hashes),
    )
    if job.obstacle_detection is not None:
        operation.ultra_wave = _ultra_wave(job.obstacle_detection, detection)
    return RoutePlan(zone_hashes=zone_hashes, settings=operation, job=job)


def _ultra_wave(mode: ObstacleDetection, detection: Sequence[DetectionStrategy]) -> int:
    """The ``ultra_wave`` value of the app's option ``mode`` among ``detection``."""
    try:
        return int(DetectionStrategy.from_option_key(mode.value, list(detection)))
    except ValueError:
        msg = f"obstacle detection {mode.value!r} is not offered by this mower"
        raise ValueError(msg) from None


def movement_wire(linear: float, angular: float) -> tuple[int, int]:
    """Normalised stick values (-1..1, left positive) to ``send_movement`` wire units."""

    def scale(value: float, per_percent: float) -> int:
        percent = min(abs(value), 1.0) * 100.0
        if percent <= DEAD_ZONE_PERCENT:
            return 0
        magnitude = int((percent - DEAD_ZONE_PERCENT) * per_percent)
        return magnitude if value > 0 else -magnitude

    return scale(linear, LINEAR_WIRE_PER_PERCENT), -scale(angular, ANGULAR_WIRE_PER_PERCENT)
