"""Worx data (:class:`~mowdeck_control_worx.snapshot.WorxMower`) to MowDeck contracts.

Activities: the mower reports a status (``dat.ls``) and an error (``dat.le``).
:func:`activity_of` maps both onto one :class:`~mowdeck_contracts.v1.MowerActivity`, in this
order (the full table is in the package README):

1. the cloud reports the mower offline: ``offline``;
2. an error code other than "no error" and the rain delay (5): ``error``; so are the statuses
   trapped (9) and blade blocked (10) without one;
3. the status by :data:`ACTIVITY_BY_STATUS`; "follow wire" (4) is ``returning`` when the mower
   was returning before and ``mowing`` otherwise (the mower follows its wire to a zone's start
   and on its way home alike);
4. home (1) while charging: ``charging``.

Theft alarm (ADR 0027): the error codes lifted (2) and upside down (11) set ``raw.lifted`` and
pass the code as ``raw.lift_code``; their lexicon category is ``lift_tilt``. Blades: the cloud
does not report them; ``raw.blade_on`` is True exactly in the statuses that cut (mowing, cutting
edge, border crossing) and marked ``raw.blade_on_source = "status"``.
"""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from mowdeck_contracts.v1 import (
    ConnectionKind,
    DeviceType,
    DiscoveredDevice,
    GeoPosition,
    GeoReference,
    MowerActivity,
    MowerCapabilities,
    MowerErrorCategory,
    MowerMap,
    MowerState,
    PositionSource,
    SettingGroup,
    SettingSpec,
    SettingType,
    SignalStatus,
    StartPlan,
    StartPlanKind,
)

from mowdeck_control_worx import lexicon
from mowdeck_control_worx.snapshot import WorxMower

LIFT_CODES: Final = lexicon.LIFT_TILT_CODES
"""Lifted (2) and upside down (11) (ADR 0066)."""

STATUS_FOLLOW_WIRE: Final = 4
STATUS_HOME: Final = 1

ACTIVITY_BY_STATUS: Final[Mapping[int, MowerActivity]] = {
    0: MowerActivity.IDLE,
    1: MowerActivity.DOCKED,
    2: MowerActivity.MOWING,
    3: MowerActivity.MOWING,
    5: MowerActivity.RETURNING,
    6: MowerActivity.RETURNING,
    7: MowerActivity.MOWING,
    8: MowerActivity.MOWING,
    9: MowerActivity.ERROR,
    10: MowerActivity.ERROR,
    13: MowerActivity.MOWING,
    30: MowerActivity.RETURNING,
    31: MowerActivity.MOWING,
    32: MowerActivity.MOWING,
    33: MowerActivity.MOWING,
    34: MowerActivity.PAUSED,
    103: MowerActivity.MOWING,
    104: MowerActivity.RETURNING,
    110: MowerActivity.MOWING,
    111: MowerActivity.MOWING,
}
"""Statuses (``dat.ls``) to activities; debug (11), remote control (12) and unknown ones are
``unknown``. Zoning (31, the zone training) and the searches drive with the blades off but are
part of a job, so they count as ``mowing``."""

CUTTING_STATUSES: Final = frozenset({7, 8, 32, 110})
"""Statuses in which the blades turn: mowing, cutting edge, border crossing."""

_STATUS_CATEGORY: Final[Mapping[int, MowerErrorCategory]] = {
    9: MowerErrorCategory.TRAPPED,
    10: MowerErrorCategory.BLADE_MOTOR,
}

BLADE_HEIGHT_RANGE_MM: Final = (30, 60)
"""The cutting height module's range (assumed from the brands' specifications of the models
with an electronic height adjustment; not confirmed through the API)."""
BLADE_HEIGHT_STEP_MM: Final = 5

RAIN_DELAY_SETTING: Final = "rain_delay"
RAIN_DELAY_MAX_MIN: Final = 1440
"""pyworxcloud accepts 0 to 1440 minutes."""

PLAN_NOW: Final = "now"
PLAN_EDGE: Final = "edge"
PLAN_ZONE: Final = "zone"


def activity_of(mower: WorxMower, previous: MowerActivity | None = None) -> MowerActivity:
    """The MowDeck activity (see the module docstring); ``previous`` resolves "follow wire"."""
    if not mower.online:
        return MowerActivity.OFFLINE
    if lexicon.is_error(mower.error_code):
        return MowerActivity.ERROR
    status = mower.status_code
    if status is None:
        return MowerActivity.UNKNOWN
    if status == STATUS_FOLLOW_WIRE:
        return (
            MowerActivity.RETURNING if previous is MowerActivity.RETURNING else MowerActivity.MOWING
        )
    activity = ACTIVITY_BY_STATUS.get(status, MowerActivity.UNKNOWN)
    if activity is MowerActivity.DOCKED and mower.charging:
        return MowerActivity.CHARGING
    return activity


def blade_on_of(mower: WorxMower, activity: MowerActivity) -> bool:
    """Derived: the blades turn in the cutting statuses of a mowing mower."""
    return activity is MowerActivity.MOWING and mower.status_code in CUTTING_STATUSES


def error_of(
    mower: WorxMower, activity: MowerActivity
) -> tuple[str | None, str | None, MowerErrorCategory | None]:
    """``(error_code, error_message, error_category)`` of an ``error`` activity."""
    if activity is not MowerActivity.ERROR:
        return None, None, None
    code = mower.error_code
    if lexicon.is_error(code):
        title = lexicon.ENGLISH_TITLES.get(code, f"Unknown error {code}")
        return str(code), title, lexicon.category_of(code)
    status = mower.status_code
    name = lexicon.STATUS_NAMES.get(status) if status is not None else None
    message = name[:1].upper() + name[1:] if name else None
    category = _STATUS_CATEGORY.get(status) if status is not None else None
    return None, message, category or MowerErrorCategory.OTHER


def position_of(mower: WorxMower) -> tuple[GeoPosition | None, PositionSource]:
    """The GPS module's position (``gnss``) or an RTK model's (quality unknown)."""
    found = mower.gps
    if found is None:
        return None, PositionSource.UNKNOWN
    lat, lon, source = found
    kind = PositionSource.GNSS if source == "4g" else PositionSource.UNKNOWN
    return GeoPosition(lat=lat, lon=lon), kind


def _statistics(mower: WorxMower) -> dict[str, Any]:
    """The lifetime counters Telemetry reads (``blade_used_s``, ``bat_cycles``, ...)."""
    stats, raw = mower.statistics, {}
    blade, work, distance = stats.get("b"), stats.get("wt"), stats.get("d")
    if isinstance(blade, int) and not isinstance(blade, bool) and blade >= 0:
        raw["blade_used_s"] = blade * 60
    if isinstance(work, int) and not isinstance(work, bool) and work >= 0:
        raw["work_time_s"] = work * 60
    if isinstance(distance, int) and not isinstance(distance, bool) and distance >= 0:
        raw["mileage_m"] = distance
    cycles = mower.charge_cycles
    if cycles is not None and cycles >= 0:
        raw["bat_cycles"] = cycles
    return raw


def _raw(mower: WorxMower, activity: MowerActivity) -> dict[str, Any]:
    status = mower.status_code
    raw: dict[str, Any] = {
        "vendor": "worx",
        "worx_status": status,
        "worx_status_name": lexicon.STATUS_NAMES.get(status) if status is not None else None,
        "worx_protocol": mower.protocol,
        "blade_on": blade_on_of(mower, activity),
        "blade_on_source": "status",
    }
    code = mower.error_code
    if activity is MowerActivity.ERROR and lexicon.is_error(code):
        if code in LIFT_CODES:
            raw["lifted"] = True
            raw["lift_code"] = str(code)
    elif code not in lexicon.NO_ERROR_CODES:
        raw["last_error_code"] = str(code)
    if mower.rain_remaining_min is not None:
        raw["rain_delay_remaining_min"] = mower.rain_remaining_min
    zone = mower.current_zone
    if zone is not None and mower.zones:
        raw["zone"] = zone + 1
    if mower.locked is not None:
        raw["locked"] = mower.locked
    raw.update(_statistics(mower))
    return raw


def settings_of(mower: WorxMower) -> dict[str, Any]:
    """``MowerState.settings``: the rain delay where the mower reports it."""
    delay = mower.rain_delay_min
    return {RAIN_DELAY_SETTING: min(delay, RAIN_DELAY_MAX_MIN)} if delay is not None else {}


def state_of(
    mower: WorxMower,
    *,
    mower_id: UUID,
    account_id: UUID,
    observed_at: datetime | None = None,
    previous: MowerState | None = None,
) -> MowerState:
    """The MowDeck state of one mower. ``observed_at`` is when its data last changed (the
    session's time); ``previous`` is the last state (its activity resolves "follow wire", its
    battery stands in while a report carries none)."""
    activity = activity_of(mower, previous.activity if previous is not None else None)
    code, message, category = error_of(mower, activity)
    position, source = position_of(mower)
    battery = mower.battery_percent
    if battery is None and previous is not None:
        battery = previous.battery_percent
    wifi = mower.wifi_dbm
    return MowerState(
        mower_id=mower_id,
        account_id=account_id,
        observed_at=observed_at or datetime.now(UTC),
        activity=activity,
        battery_percent=battery if battery is not None else 0,
        charging=mower.charging,
        position=position,
        position_source=source,
        connection=ConnectionKind.CLOUD,
        blade_height_mm=mower.cutting_height_mm if mower.has_cutting_height else None,
        error_code=code,
        error_message=message,
        error_category=category,
        firmware=mower.firmware,
        signal=SignalStatus(wifi_dbm=wifi) if wifi is not None else None,
        rain_detected=mower.rain_sensor_wet,
        settings=settings_of(mower),
        raw=_raw(mower, activity),
    )


def start_plans_of(mower: WorxMower, *, offer_zones: bool = True) -> list[StartPlan]:
    """The starts the mower offers: now, each usable zone (numbered like the app, 1 to 4) and
    the edge cut where the mower has the one-time schedule."""
    plans = [StartPlan(ref=PLAN_NOW, kind=StartPlanKind.NOW)]
    if offer_zones:
        plans.extend(
            StartPlan(ref=f"{PLAN_ZONE}:{z + 1}", kind=StartPlanKind.ZONE, name=f"Zone {z + 1}")
            for z in mower.zones
        )
    if mower.has_edge_cut:
        plans.append(StartPlan(ref=PLAN_EDGE, kind=StartPlanKind.EDGE))
    return plans


RAIN_DELAY_SPEC: Final = SettingSpec(
    key=RAIN_DELAY_SETTING,
    type=SettingType.INT,
    group=SettingGroup.WEATHER,
    min=0,
    max=RAIN_DELAY_MAX_MIN,
    unit="min",
)


def capabilities_of(mower: WorxMower | None, *, offer_zones: bool = True) -> MowerCapabilities:
    """Honest capabilities: only what the cloud API offers for this mower.

    No native patterns, manual driving, camera, light, RTK reporting or custom paths. The blade
    height only with the cutting height module, the rain delay only where the mower reports one,
    and the start plans instead of map areas.
    """
    if mower is None:
        return MowerCapabilities(connection_kinds=frozenset({ConnectionKind.CLOUD}))
    settings = [RAIN_DELAY_SPEC] if mower.rain_delay_min is not None else []
    return MowerCapabilities(
        model=mower.model[:64] if mower.model else None,
        connection_kinds=frozenset({ConnectionKind.CLOUD}),
        blade_height_range_mm=BLADE_HEIGHT_RANGE_MM if mower.has_cutting_height else None,
        settings=settings,
        start_plans=start_plans_of(mower, offer_zones=offer_zones),
    )


def map_of(reference: GeoPosition | None) -> MowerMap:
    """A map without geometry (the cloud has none): only the geo reference, at the first known
    position of the mower (or 0/0 for models without GPS)."""
    lat, lon = (reference.lat, reference.lon) if reference is not None else (0.0, 0.0)
    return MowerMap(
        reference=GeoReference(origin_lat=lat, origin_lon=lon), revision="worx-no-geometry"
    )


def device_names(mowers: Mapping[str, WorxMower]) -> dict[str, str]:
    """A unique display name per serial number: the mower's name, with the serial number added
    when two mowers of the account share a name."""
    counts: dict[str, int] = {}
    for mower in mowers.values():
        counts[mower.name] = counts.get(mower.name, 0) + 1
    names: dict[str, str] = {}
    for serial, mower in mowers.items():
        name = f"{mower.name} ({serial})" if counts[mower.name] > 1 else mower.name
        names[serial] = name[:100]
    return names


def discovered_devices(mowers: Mapping[str, WorxMower]) -> list[DiscoveredDevice]:
    """The account's mowers as :class:`~mowdeck_contracts.v1.DiscoveredDevice`, by name."""
    names = device_names(mowers)
    found = [
        DiscoveredDevice(
            device_name=names[serial],
            iot_id=serial,
            model=mower.model,
            online=mower.online,
            is_mower=True,
            device_type=DeviceType.MOWER,
        )
        for serial, mower in mowers.items()
    ]
    return sorted(found, key=lambda d: d.device_name.casefold())
