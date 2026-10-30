"""The capability profiles of the Mammotion provider (ADR 0071).

What a Mammotion mower offers depends on its model family (``DeviceType`` from the device
name), its exact model (the sub model in PyMammotion's model table: blade height and speed
ranges), its firmware (some settings and obstacle detection modes) and the links it has (the
Bluetooth bridge adds reverse driving). Each profile is the capabilities
:func:`~mowdeck_control_mammotion.mapping.capabilities_of` computes for a representative mower of
the family, with the same arguments a dual connection (cloud plus Bluetooth bridge) passes
while every link is up and every option is on, and a firmware new enough for every
firmware-gated setting. What depends on the exact model or firmware is listed as ``varies``.

``not_offered`` is what the mower family itself does not have (no camera and no light on the
LUBA 1) or what neither the vendor's protocol nor PyMammotion offers (arbitrary paths, a local
network API).
"""

from dataclasses import dataclass
from typing import Final

from mowdeck_contracts.v1 import (
    LINK_BLE_BRIDGE,
    CapabilityProfile,
    ProfileFeature,
    ProfileReport,
)

from mowdeck_control_mammotion import mapping, settings
from mowdeck_control_mammotion.port import DeviceType, MowerDevice

PROFILE_FIRMWARE: Final = "99.0.0"
"""A firmware newer than every firmware gate, so a profile shows what the newest firmware
offers (``varies`` names what older firmware lacks)."""


@dataclass(frozen=True, slots=True)
class Family:
    """One model family: its device types and the exact model the profile is computed for."""

    family: str
    name: str
    types: tuple[DeviceType, ...]
    sub_model: str
    """A key of PyMammotion's model table (the mower answers it after connecting)."""
    tested_on_hardware: bool = False

    @property
    def prefixes(self) -> list[str]:
        """The device name prefixes of the family's types."""
        found: list[str] = []
        for device_type in self.types:
            for prefix in str(device_type.value[1]).split(","):
                if prefix.strip() and prefix.strip() not in found:
                    found.append(prefix.strip())
        return found

    @property
    def device_names(self) -> list[str]:
        """One device name per prefix (the prefix and a serial), the representative first."""
        return [f"{p}{'' if '-' in p else '-'}0000" for p in self.prefixes]

    @property
    def device_name(self) -> str:
        """The device name of the representative mower."""
        return self.device_names[0]


FAMILIES: Final = (
    Family("luba_1", "LUBA 1", (DeviceType.LUBA,), "HM050080LBAWD50"),
    Family("luba_2_awd", "LUBA 2 AWD", (DeviceType.LUBA_2,), "HM030070LBVAWD50OMNI"),
    Family(
        "luba_2_awd_x",
        "LUBA 2 AWD X",
        (DeviceType.LUBA_VP,),
        "HM030070LB2PAWD50OMNI",
        tested_on_hardware=True,
    ),
    Family(
        "luba_mini_and_3",
        "LUBA mini and LUBA 3",
        (
            DeviceType.LUBA_MN,
            DeviceType.LUBA_LD,
            DeviceType.LUBA_VA,
            DeviceType.LUBA_MD,
            DeviceType.LUBA_LA,
            DeviceType.LUBA_MB,
            DeviceType.LUBA_HM,
            DeviceType.LUBA_ME,
            DeviceType.LUBA_SP,
            DeviceType.LUBA_TR,
            DeviceType.LUBA_LS,
            DeviceType.LUBA_MS,
        ),
        "HM020065LB2MINIAWD15OMNI",
    ),
    Family("yuka", "Yuka", (DeviceType.LUBA_YUKA,), "HM030070YK15"),
    Family(
        "yuka_mini",
        "Yuka mini",
        (
            DeviceType.YUKA_MINI,
            DeviceType.YUKA_MINI2,
            DeviceType.YUKA_VP,
            DeviceType.YUKA_MINIV,
            DeviceType.YUKA_ML,
            DeviceType.YUKA_MN100,
            DeviceType.YUKA_MN101,
            DeviceType.YUKA_HS,
        ),
        "HM020080YKMINI08",
    ),
)
"""The families the matrix shows, in this order. Mammotion's pool cleaners and RTK base
stations are no mowers and have no profile."""

_REPORTS: Final = frozenset(
    {
        ProfileReport.POSITION,
        ProfileReport.MAP_GEOMETRY,
        ProfileReport.JOB_PROGRESS,
        ProfileReport.BLADE_HEIGHT,
        ProfileReport.SATELLITES,
        ProfileReport.SIGNAL,
        ProfileReport.ERROR_CODES,
        ProfileReport.LIFT_DETECTION,
        ProfileReport.BATTERY_CYCLES,
        ProfileReport.FIRMWARE_VERSION,
        ProfileReport.BASE_STATION,
    }
)

_ALWAYS_VARIES: Final = frozenset(
    {
        ProfileFeature.BLADE_HEIGHT.value,
        ProfileFeature.MOWING_SPEED.value,
        ProfileFeature.PATH_SPACING.value,
        ProfileFeature.MANUAL_DRIVE.value,
        ProfileFeature.DRIVE_REVERSE.value,
        ProfileFeature.MANUAL_DRIVE_BLADES.value,
        ProfileFeature.SPOT_MOWING.value,
        ProfileFeature.BASE_STATION_FIRMWARE_UPDATE.value,
        ProfileReport.BASE_STATION.value,
    }
)
"""Keys that depend on the exact model (ranges), on the links and options (manual driving needs
the Bluetooth bridge or the cloud drive option, reverse driving and blades the bridge and the
manual blades option) or on a linked RTK base station."""

_FIRMWARE_GATED: Final = frozenset(
    {
        "wildlife_safety",
        "recharge_level",
        "smart_recharge_level",
        "resume_level",
        "smart_resume_level",
        ProfileFeature.START_OPTIONS.value,
    }
)
"""Keys gated by the firmware (ADR 0046, ADR 0030); they vary where the family declares them."""

_NOTES: Final = [
    {
        "en": "Manual driving and spot runs need the Bluetooth bridge or the cloud "
        "drive option; reverse driving and blades while driving need the bridge.",
        "de": "Manuelles Fahren und Spot-LÃ¤ufe brauchen die Bluetooth-BrÃ¼cke oder die Option "
        "Fahren Ã¼ber die Cloud; RÃ¼ckwÃ¤rtsfahren und Messer beim Fahren brauchen die BrÃ¼cke.",
    },
    {
        "en": "Blade height, speed and path spacing ranges differ per exact model; the values "
        "shown are those of one model of the family.",
        "de": "SchnitthÃ¶he, Geschwindigkeit und Bahnabstand unterscheiden sich je nach genauem "
        "Modell; gezeigt sind die Werte eines Modells der Familie.",
    },
    {
        "en": "Some settings and obstacle detection modes need a recent firmware.",
        "de": "Manche Einstellungen und Hinderniserkennungs-Modi brauchen eine aktuelle Firmware.",
    },
]


def representative_device(family: Family) -> MowerDevice:
    """A mower of ``family`` that has answered the model question with its sub model."""
    device = MowerDevice(name=family.device_name)
    device.mower_state.sub_model_id = family.sub_model
    return device


def _model_keys(device_name: str) -> frozenset[str]:
    """The keys that differ between the models of a family: settings, camera and light."""
    keys = {spec.key for spec in settings.settings_of(device_name, PROFILE_FIRMWARE)}
    if mapping.has_camera(device_name):
        keys.add(ProfileFeature.CAMERA.value)
    if mapping.has_light(device_name):
        keys.add(ProfileFeature.LIGHT.value)
    return frozenset(keys)


def profile_of(family: Family) -> CapabilityProfile:
    """The profile of ``family`` (see the module docstring).

    Settings, camera and light are checked for every device type of the family: what only some
    of them have varies, what none has is not offered (camera and light) or missing."""
    name = family.device_name
    caps = mapping.capabilities_of(
        representative_device(family),
        device_name=name,
        product_key="",
        manual_drive=True,
        manual_drive_blades=True,
        drive_reverse=True,
        camera=True,
        model=family.name,
        links_supported=[LINK_BLE_BRIDGE],
        firmware_update=True,
        undock=True,
        drive_speed=True,
        settings=settings.settings_of(name, PROFILE_FIRMWARE),
        maintenance=True,
        firmware=PROFILE_FIRMWARE,
    ).model_copy(update={"base_station_firmware_update": True})
    per_model = [_model_keys(member) for member in family.device_names]
    some, every = frozenset().union(*per_model), frozenset.intersection(*per_model)
    not_offered = {ProfileFeature.CUSTOM_PATHS.value, ProfileFeature.TRANSPORT_WIFI.value}
    for hardware in (ProfileFeature.CAMERA.value, ProfileFeature.LIGHT.value):
        if hardware not in some:
            not_offered.add(hardware)
    reports = set(_REPORTS)
    if "rain_detection" in some:
        reports.add(ProfileReport.RAIN_SENSOR)
    varies = (
        _ALWAYS_VARIES
        | (some - every)
        | (_FIRMWARE_GATED & (some | {ProfileFeature.START_OPTIONS.value}))
    )
    if ProfileReport.RAIN_SENSOR in reports:
        varies |= {ProfileReport.RAIN_SENSOR.value}
    return CapabilityProfile(
        family=family.family,
        name=family.name,
        models=family.prefixes,
        capabilities=caps,
        reports=frozenset(reports),
        varies=frozenset(varies),
        not_offered=frozenset(not_offered),
        tested_on_hardware=family.tested_on_hardware,
        notes=list(_NOTES),
    )


def capability_profiles() -> list[CapabilityProfile]:
    """The provider's model families (:data:`FAMILIES`)."""
    return [profile_of(family) for family in FAMILIES]
