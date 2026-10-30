"""Capability profiles: what MowDeck offers on one model family of a provider (ADR 0071).

``MowerCapabilities`` is what one connected mower declares at run time; it depends on the exact
model, firmware and modules the mower reports after connecting. A :class:`CapabilityProfile` is
the static counterpart for documentation and for a client that wants to show what a provider can
do before an account is linked: an adapter declares one profile per model family, each with the
capabilities a representative mower of the family declares (computed by the adapter's own
capability code, not written by hand), the state it reports, and for everything it does not
offer whether the mower or its vendor's API cannot do it (``not_offered``) or MowDeck simply
does not support it yet (everything else).

The keys of a profile are neutral words, so a client labels them without knowing the vendor:

* :class:`ProfileFeature`, derived from ``MowerCapabilities`` (:func:`profile_features`);
* :class:`ProfileReport`, the state a family reports, declared by the adapter;
* the setting keys of ``SETTING_KEYS`` (ADR 0046), declared in ``capabilities.settings``.
"""

from collections.abc import Iterable
from enum import StrEnum
from typing import Annotated, Final, Self

from pydantic import Field, PlainSerializer, StringConstraints, model_validator

from mowdeck_contracts.v1._base import ContractModel
from mowdeck_contracts.v1.capabilities import CameraAccess, MowerCapabilities, StartOptions
from mowdeck_contracts.v1.mower import ConnectionKind
from mowdeck_contracts.v1.providers import LocalizedText
from mowdeck_contracts.v1.settings import SETTING_KEYS

FAMILY_ID_PATTERN: Final = r"^[a-z][a-z0-9_]{0,39}$"
"""A model family id: lower case, digits and underscores, unique within its provider."""


class ProfileFeature(StrEnum):
    """What a mower can be told to do or offers, derived from ``MowerCapabilities``."""

    TRANSPORT_CLOUD = "transport_cloud"
    """Reached through the vendor's cloud (``connection_kinds`` holds ``cloud``)."""
    TRANSPORT_BLE_BRIDGE = "transport_ble_bridge"
    """Reached over Bluetooth through a bridge near the garden (a connection kind or an
    additional link)."""
    TRANSPORT_WIFI = "transport_wifi"
    """Reached directly on the local network."""
    NATIVE_PATTERNS = "native_patterns"
    """Mowing patterns the mower runs on its own map areas."""
    START_OPTIONS = "start_options"
    """Job options of a start (obstacle detection, laps around no-go zones, order, progress)."""
    START_PLANS = "start_plans"
    """Starts without map areas (now, a vendor zone, the edge, the mower's schedule)."""
    DOCK_KEEPS_JOB = "dock_keeps_job"
    """Return to the dock and resume the same job later."""
    BLADE_HEIGHT = "blade_height"
    """Blade height settable in millimetres."""
    PATH_SPACING = "path_spacing"
    """Distance between mowing paths settable."""
    MOWING_SPEED = "mowing_speed"
    """Driving speed while mowing settable."""
    MANUAL_DRIVE = "manual_drive"
    """Joystick driving."""
    MANUAL_DRIVE_BLADES = "manual_drive_blades"
    """Blades may run while driving manually."""
    DRIVE_REVERSE = "drive_reverse"
    """The drive link can drive backwards."""
    DRIVE_SPEED_SETTING = "drive_speed_setting"
    """The vendor's speed setting for manual driving can be read and changed."""
    SPOT_MOWING = "spot_mowing"
    """Spot runs and drive calibration, driven by MowDeck (needs manual driving)."""
    UNDOCK = "undock"
    """The mower leaves its dock on command (a spot run may start from the dock)."""
    CUSTOM_PATHS = "custom_paths"
    """The mower follows an arbitrary path itself."""
    LIGHT = "light"
    """A light switched with ``set_light``."""
    CAMERA = "camera"
    """A live camera stream."""
    RTK = "rtk"
    """RTK correction sources reported."""
    FIRMWARE_UPDATE = "firmware_update"
    """Firmware updates checked and installed."""
    BASE_STATION_FIRMWARE_UPDATE = "base_station_firmware_update"
    """Firmware updates of the linked RTK base station."""
    MAINTENANCE = "maintenance"
    """Maintenance commands (restart, map sync, dock relocation, RTK resync, ...)."""


class ProfileReport(StrEnum):
    """What a model family reports in its state, declared by the adapter."""

    POSITION = "position"
    """A position on the map or in WGS84 (``MowerState.position``)."""
    MAP_GEOMETRY = "map_geometry"
    """Map areas with geometry (boundaries, no-go zones, paths)."""
    JOB_PROGRESS = "job_progress"
    """Progress of the running job (``MowerState.job``)."""
    BLADE_HEIGHT = "blade_height_state"
    """The current blade height (``MowerState.blade_height_mm``)."""
    SATELLITES = "satellites"
    """Satellite and RTK fix details (``MowerState.gnss``, ``rtk_fix``)."""
    SIGNAL = "signal"
    """Radio signal of the mower's links (Wi-Fi, mobile, Bluetooth; ``MowerState.signal``)."""
    ERROR_CODES = "error_codes"
    """Vendor error codes, explained by the error lexicon (ADR 0067)."""
    LIFT_DETECTION = "lift_detection"
    """Lift and tilt alarms (``raw.lifted``, the theft alarm)."""
    RAIN_SENSOR = "rain_sensor"
    """The rain sensor (``MowerState.rain_detected``)."""
    BATTERY_CYCLES = "battery_cycles"
    """The device's own battery cycle counter (``raw.bat_cycles``)."""
    FIRMWARE_VERSION = "firmware_version"
    """The installed firmware version (``MowerState.firmware``)."""
    BASE_STATION = "base_station"
    """State of the linked RTK base station (``MowerState.base_station``)."""


PROFILE_KEYS: Final[frozenset[str]] = frozenset(
    {*(f.value for f in ProfileFeature), *(r.value for r in ProfileReport), *SETTING_KEYS}
)
"""Every key a profile can mark as varying or not offered."""


def _offers_start_options(options: StartOptions) -> bool:
    return bool(
        options.obstacle_detection
        or options.obstacle_laps_range
        or options.orders
        or options.start_progress_range_pct
    )


def profile_features(capabilities: MowerCapabilities) -> frozenset[ProfileFeature]:
    """The features ``capabilities`` declare, in the words of :class:`ProfileFeature`."""
    kinds = {str(k) for k in capabilities.connection_kinds} | set(capabilities.links_supported)
    checks: dict[ProfileFeature, bool] = {
        ProfileFeature.TRANSPORT_CLOUD: ConnectionKind.CLOUD in kinds,
        ProfileFeature.TRANSPORT_BLE_BRIDGE: ConnectionKind.BLE_BRIDGE in kinds,
        ProfileFeature.TRANSPORT_WIFI: ConnectionKind.WIFI in kinds,
        ProfileFeature.NATIVE_PATTERNS: bool(capabilities.native_patterns),
        ProfileFeature.START_OPTIONS: _offers_start_options(capabilities.start_options),
        ProfileFeature.START_PLANS: bool(capabilities.start_plans),
        ProfileFeature.DOCK_KEEPS_JOB: capabilities.dock_keeps_job,
        ProfileFeature.BLADE_HEIGHT: capabilities.blade_height_range_mm is not None,
        ProfileFeature.PATH_SPACING: capabilities.path_spacing_range_mm is not None,
        ProfileFeature.MOWING_SPEED: capabilities.max_speed_mps is not None,
        ProfileFeature.MANUAL_DRIVE: capabilities.manual_drive,
        ProfileFeature.MANUAL_DRIVE_BLADES: capabilities.manual_drive_blades,
        ProfileFeature.DRIVE_REVERSE: capabilities.drive_reverse,
        ProfileFeature.DRIVE_SPEED_SETTING: capabilities.drive_speed_setting is not None,
        ProfileFeature.SPOT_MOWING: capabilities.manual_drive,
        ProfileFeature.UNDOCK: capabilities.undock,
        ProfileFeature.CUSTOM_PATHS: capabilities.custom_paths,
        ProfileFeature.LIGHT: capabilities.light,
        ProfileFeature.CAMERA: capabilities.camera is not CameraAccess.NONE,
        ProfileFeature.RTK: bool(capabilities.rtk_sources),
        ProfileFeature.FIRMWARE_UPDATE: capabilities.firmware_update,
        ProfileFeature.BASE_STATION_FIRMWARE_UPDATE: capabilities.base_station_firmware_update,
        ProfileFeature.MAINTENANCE: bool(capabilities.maintenance),
    }
    return frozenset(feature for feature, offered in checks.items() if offered)


def _sorted(values: frozenset[str]) -> list[str]:
    return sorted(values)


ProfileKeys = Annotated[
    frozenset[str], PlainSerializer(_sorted, return_type=list[str], when_used="always")
]
"""A set of profile keys, serialised as a sorted list."""


def _sorted_reports(values: frozenset[ProfileReport]) -> list[ProfileReport]:
    return sorted(values)


ProfileReports = Annotated[
    frozenset[ProfileReport],
    PlainSerializer(_sorted_reports, return_type=list[ProfileReport], when_used="always"),
]
"""A set of reports, serialised as a sorted list."""

ModelName = Annotated[str, StringConstraints(min_length=1, max_length=80)]


class CapabilityProfile(ContractModel):
    """One model family of a provider and what MowDeck offers on it (ADR 0071).

    A key a profile neither supports, marks as ``varies`` nor as ``not_offered`` is one MowDeck
    does not support yet for the family.
    """

    family: str = Field(pattern=FAMILY_ID_PATTERN, description="Unique within the provider.")
    name: str = Field(min_length=1, max_length=80, description="Display name of the family.")
    models: list[ModelName] = Field(
        default_factory=list,
        max_length=40,
        description="The models (or device name prefixes) the family covers.",
    )
    capabilities: MowerCapabilities = Field(
        description="What a representative, fully equipped mower of the family declares over "
        "its best connection, computed by the adapter's capability code."
    )
    reports: ProfileReports = Field(
        default_factory=frozenset, description="The state the family reports."
    )
    varies: ProfileKeys = Field(
        default_factory=frozenset,
        description="Keys whose availability or range depends on the exact model, firmware, "
        "add-on module or account setup (see `notes`).",
    )
    not_offered: ProfileKeys = Field(
        default_factory=frozenset,
        description="Keys the mower or its vendor's API does not offer, so MowDeck cannot "
        "either. Every other missing key is not supported by MowDeck yet.",
    )
    tested_on_hardware: bool = Field(
        default=False,
        description="A mower of this family was used with MowDeck on real hardware.",
    )
    notes: list[LocalizedText] = Field(default_factory=list, max_length=8)

    @property
    def supported(self) -> frozenset[str]:
        """Every key the representative mower offers (features, reports and settings)."""
        return frozenset(
            {
                *(f.value for f in profile_features(self.capabilities)),
                *(r.value for r in self.reports),
                *(s.key for s in self.capabilities.settings),
            }
        )

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        unknown = sorted((self.varies | self.not_offered) - PROFILE_KEYS)
        if unknown:
            msg = f"unknown profile keys: {', '.join(unknown)}"
            raise ValueError(msg)
        both = sorted(self.varies & self.not_offered)
        if both:
            msg = f"keys cannot both vary and be not offered: {', '.join(both)}"
            raise ValueError(msg)
        offered = sorted(self.not_offered & self.supported)
        if offered:
            msg = f"keys marked not offered are supported: {', '.join(offered)}"
            raise ValueError(msg)
        if len(set(self.models)) != len(self.models):
            msg = "models must not list a model twice"
            raise ValueError(msg)
        return self


def check_profiles(profiles: Iterable[CapabilityProfile]) -> list[str]:
    """What is wrong with the profiles of one provider (duplicate families); empty when fine."""
    seen: set[str] = set()
    problems: list[str] = []
    for profile in profiles:
        if profile.family in seen:
            problems.append(f"family {profile.family!r} is declared twice")
        seen.add(profile.family)
    return problems
