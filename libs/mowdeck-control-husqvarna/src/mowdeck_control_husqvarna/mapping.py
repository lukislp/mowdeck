"""Husqvarna data (aioautomower's ``MowerAttributes``) to MowDeck contracts.

Activities: the Automower Connect API reports a ``state`` (``in_operation``, ``paused``,
``restricted``, ``error``, ...) and an ``activity`` (``mowing``, ``going_home``, ``charging``,
...). :func:`activity_of` maps both onto one :class:`~mowdeck_contracts.v1.MowerActivity`, in
this order (the full table is in the package README):

1. not connected to the Husqvarna cloud: ``offline``;
2. ``error``, ``fatal_error``, ``error_at_power_up``, or ``stopped`` with an error code:
   ``error``;
3. ``stopped`` (needs manual action on the mower): ``idle``;
4. ``wait_updating``: ``updating``; ``paused``: ``paused``;
5. then the activity: ``mowing`` and ``leaving``: ``mowing``, ``going_home``: ``returning``,
   ``charging``: ``charging``, ``parked_in_cs``: ``docked``, ``stopped_in_garden``: ``idle``;
6. ``off`` without one of those activities: ``idle``; anything else ``unknown``.

Theft alarm (ADR 0027): an error with one of :data:`LIFT_CODES` sets ``raw.lifted`` and passes
the code as ``raw.lift_code``. Blades: the API does not report them; ``raw.blade_on`` is True
exactly while the mower is mowing in operation and marked ``raw.blade_on_source = "activity"``.
Positions: the newest of the mower's GPS positions (models with ``capabilities.position``),
without a quality, so ``position_source`` stays ``unknown``.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from aioautomower.model import (
    MowerActivities,
    MowerAttributes,
    MowerStates,
)
from aioautomower.model.model_mower import ERRORCODES
from mowdeck_contracts.v1 import (
    ConnectionKind,
    DeviceType,
    DiscoveredDevice,
    GeoPosition,
    GeoReference,
    MowerActivity,
    MowerCapabilities,
    MowerMap,
    MowerState,
    StartPlan,
    StartPlanKind,
)

from mowdeck_control_husqvarna import lexicon

PLAN_SCHEDULE: Final = "schedule"
"""``StartMow.plan_ref`` of a return to the mower's schedule."""
PLAN_DURATION: Final = "duration"
"""``StartMow.plan_ref`` of a start for a number of minutes (``duration[:<minutes>]``)."""
PLAN_WORK_AREA: Final = "work_area"
"""``StartMow.plan_ref`` of a start in a work area (``work_area:<id>[:<minutes>]``)."""
MAX_START_PLANS: Final = 16
"""``MowerCapabilities.start_plans`` holds at most this many."""

LIFT_CODES: Final = frozenset({14, 15, 71, 131})
"""Mower lifted, Lifted, Alarm! Mower lifted, Lifted in link arm (ADR 0066)."""

_ERROR_STATES: Final = frozenset(
    {MowerStates.ERROR, MowerStates.FATAL_ERROR, MowerStates.ERROR_AT_POWER_UP}
)

_BY_ACTIVITY: Final[Mapping[MowerActivities, MowerActivity]] = {
    MowerActivities.MOWING: MowerActivity.MOWING,
    MowerActivities.LEAVING: MowerActivity.MOWING,
    MowerActivities.GOING_HOME: MowerActivity.RETURNING,
    MowerActivities.CHARGING: MowerActivity.CHARGING,
    MowerActivities.PARKED_IN_CS: MowerActivity.DOCKED,
    MowerActivities.STOPPED_IN_GARDEN: MowerActivity.IDLE,
}


def activity_of(attributes: MowerAttributes) -> MowerActivity:
    """The MowDeck activity (see the module docstring)."""
    if not attributes.metadata.connected:
        return MowerActivity.OFFLINE
    mower = attributes.mower
    state = mower.state
    if state in _ERROR_STATES or (state is MowerStates.STOPPED and mower.error_code):
        return MowerActivity.ERROR
    if state is MowerStates.STOPPED:
        return MowerActivity.IDLE
    if state is MowerStates.WAIT_UPDATING:
        return MowerActivity.UPDATING
    if state is MowerStates.PAUSED:
        return MowerActivity.PAUSED
    found = _BY_ACTIVITY.get(mower.activity)
    if found is not None:
        return found
    if state is MowerStates.OFF:
        return MowerActivity.IDLE
    return MowerActivity.UNKNOWN


def blade_on_of(attributes: MowerAttributes) -> bool:
    """Derived: the blades turn while the mower mows in operation."""
    mower = attributes.mower
    return (
        attributes.metadata.connected
        and mower.state is MowerStates.IN_OPERATION
        and mower.activity is MowerActivities.MOWING
    )


def error_text(code: int) -> str:
    """Husqvarna's English text of an error code (aioautomower's table)."""
    return ERRORCODES.get(code, f"Unknown error {code}")


def position_of(attributes: MowerAttributes) -> GeoPosition | None:
    """The newest GPS position (the API lists the newest first)."""
    if not attributes.capabilities.position or not attributes.positions:
        return None
    newest = attributes.positions[0]
    return GeoPosition(lat=newest.latitude, lon=newest.longitude)


def _statistics(attributes: MowerAttributes) -> dict[str, Any]:
    """The lifetime counters Telemetry reads (``blade_used_s``, ``bat_cycles``, ...)."""
    stats = attributes.statistics
    raw: dict[str, Any] = {}
    pairs = (
        ("blade_used_s", stats.cutting_blade_usage_time),
        ("bat_cycles", stats.number_of_charging_cycles),
        ("work_time_s", stats.total_cutting_time),
        ("mileage_m", stats.total_drive_distance),
    )
    for key, value in pairs:
        if value is not None:
            raw[key] = value
    return raw


def _raw(attributes: MowerAttributes, activity: MowerActivity) -> dict[str, Any]:
    mower, planner = attributes.mower, attributes.planner
    raw: dict[str, Any] = {
        "vendor": "husqvarna",
        "husqvarna_state": mower.state.value,
        "husqvarna_activity": mower.activity.value,
        "husqvarna_mode": mower.mode.value,
        "inactive_reason": mower.inactive_reason.value,
        "restricted_reason": planner.restricted_reason.value,
        "override": planner.override.action.value,
        "blade_on": blade_on_of(attributes),
        "blade_on_source": "activity",
    }
    if planner.next_start_datetime is not None:
        raw["next_start"] = planner.next_start_datetime.isoformat()
    if mower.work_area_id is not None:
        raw["work_area_id"] = mower.work_area_id
    if mower.work_area_name is not None:
        raw["work_area_name"] = mower.work_area_name
    if attributes.settings.cutting_height is not None:
        raw["cutting_height_level"] = attributes.settings.cutting_height
    if attributes.settings.headlight.mode is not None:
        raw["headlight_mode"] = attributes.settings.headlight.mode.value
    if attributes.work_areas:
        raw["work_areas"] = {
            str(area_id): area.name for area_id, area in sorted(attributes.work_areas.items())
        }
    if attributes.battery.remaining_charging_time is not None:
        raw["remaining_charging_s"] = int(
            attributes.battery.remaining_charging_time.total_seconds()
        )
    raw.update(_statistics(attributes))
    if activity is MowerActivity.ERROR and mower.error_code:
        raw["error_key"] = mower.error_key
        if mower.error_code in LIFT_CODES:
            raw["lifted"] = True
            raw["lift_code"] = str(mower.error_code)
    elif mower.error_code:
        raw["last_error_code"] = str(mower.error_code)
    return raw


def state_of(
    attributes: MowerAttributes,
    *,
    mower_id: UUID,
    account_id: UUID,
    observed_at: datetime | None = None,
) -> MowerState:
    """The MowDeck state of one mower. ``observed_at`` is the cloud's status time, or the
    later time an event changed the data (the status time does not move with every event)."""
    activity = activity_of(attributes)
    mower = attributes.mower
    error = activity is MowerActivity.ERROR and bool(mower.error_code)
    position = position_of(attributes)
    reported = attributes.metadata.status_dateteime
    return MowerState(
        mower_id=mower_id,
        account_id=account_id,
        observed_at=max(reported, observed_at) if observed_at is not None else reported,
        activity=activity,
        battery_percent=max(0, min(100, attributes.battery.battery_percent)),
        charging=mower.activity is MowerActivities.CHARGING,
        position=position,
        connection=ConnectionKind.CLOUD,
        error_code=str(mower.error_code) if error else None,
        error_message=error_text(mower.error_code) if error else None,
        error_category=(lexicon.category_of(mower.error_code) if error else None),
        raw=_raw(attributes, activity),
    )


def start_plans_of(attributes: MowerAttributes) -> list[StartPlan]:
    """The starts the mower offers without map geometry (ADR 0066 amendment): mow now for the
    option ``start_minutes`` (``duration``), each work area of a model with work areas
    (``work_area:<id>``, the vendor's name; the main area 0 often has none) and back to the
    schedule (``schedule``). :func:`~mowdeck_control_husqvarna.connection.action_for_plan`
    validates the reference."""
    plans = [StartPlan(ref=PLAN_DURATION, kind=StartPlanKind.NOW)]
    if attributes.capabilities.work_areas and attributes.work_areas:
        plans.extend(
            StartPlan(
                ref=f"{PLAN_WORK_AREA}:{area_id}",
                kind=StartPlanKind.ZONE,
                name=area.name.strip()[:64] or None,
            )
            for area_id, area in sorted(attributes.work_areas.items())[: MAX_START_PLANS - 2]
        )
    plans.append(StartPlan(ref=PLAN_SCHEDULE, kind=StartPlanKind.SCHEDULE))
    return plans


def capabilities_of(attributes: MowerAttributes | None) -> MowerCapabilities:
    """Honest capabilities: everything MowDeck could drive itself is off.

    No native patterns (the API has no pattern parameters), no manual driving, no camera, no
    light switch (headlights are a mode, not a switch), no RTK reporting, no custom paths and no
    blade height (the API's cutting height is a level from 1 to 9, not millimetres). The map has
    no areas, so the mower is started through its start plans (:func:`start_plans_of`).
    """
    return MowerCapabilities(
        model=attributes.system.model[:64] if attributes is not None else None,
        connection_kinds=frozenset({ConnectionKind.CLOUD}),
        start_plans=start_plans_of(attributes) if attributes is not None else [],
    )


def map_of(reference: GeoPosition | None) -> MowerMap:
    """A map without geometry (the API has none): only the geo reference, at the first known
    position of the mower (or 0/0 for models without GPS)."""
    lat, lon = (reference.lat, reference.lon) if reference is not None else (0.0, 0.0)
    return MowerMap(
        reference=GeoReference(origin_lat=lat, origin_lon=lon), revision="husqvarna-no-geometry"
    )


def device_names(mowers: Mapping[str, MowerAttributes]) -> dict[str, str]:
    """A unique display name per mower id: the mower's name, with the serial number added
    when two mowers of the account share a name."""
    counts: dict[str, int] = {}
    for attributes in mowers.values():
        name = attributes.system.name.strip() or attributes.system.model
        counts[name] = counts.get(name, 0) + 1
    names: dict[str, str] = {}
    for mower_id, attributes in mowers.items():
        name = attributes.system.name.strip() or attributes.system.model
        if counts[name] > 1:
            name = f"{name} ({attributes.system.serial_number})"
        names[mower_id] = name[:100]
    return names


def discovered_devices(mowers: Mapping[str, MowerAttributes]) -> list[DiscoveredDevice]:
    """The account's mowers as :class:`~mowdeck_contracts.v1.DiscoveredDevice`, by name."""
    names = device_names(mowers)
    found = [
        DiscoveredDevice(
            device_name=names[mower_id],
            iot_id=mower_id,
            model=attributes.system.model[:100] or None,
            online=attributes.metadata.connected,
            is_mower=True,
            device_type=DeviceType.MOWER,
        )
        for mower_id, attributes in mowers.items()
    ]
    return sorted(found, key=lambda d: d.device_name.casefold())
