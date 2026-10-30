"""The capability profile of the Worx provider (ADR 0071).

One family for Worx Landroid, Kress and LandXcape (one cloud, one protocol). What a mower
offers depends on its add-on modules and its setup in the app, not on a model line, so the
profile's capabilities are those of a fully equipped mower (GPS module, cutting height module,
zones, the one-time schedule, a rain delay), computed by
:func:`~mowdeck_control_worx.mapping.capabilities_of`, and the parts that depend on the
equipment are listed as ``varies``.

``not_offered`` is what the cloud API does not offer (ADR 0066 amendment): map geometry,
patterns, manual driving, a camera, a light switch, RTK details, firmware updates and
maintenance commands; a return to the dock always ends the job.
"""

from mowdeck_contracts.v1 import CapabilityProfile, ProfileFeature, ProfileReport

from mowdeck_control_worx import mapping
from mowdeck_control_worx.fake_cloud import product_item
from mowdeck_control_worx.snapshot import WorxMower

_NOT_OFFERED = frozenset(
    {
        ProfileFeature.NATIVE_PATTERNS,
        ProfileFeature.START_OPTIONS,
        ProfileFeature.DOCK_KEEPS_JOB,
        ProfileFeature.PATH_SPACING,
        ProfileFeature.MOWING_SPEED,
        ProfileFeature.MANUAL_DRIVE,
        ProfileFeature.MANUAL_DRIVE_BLADES,
        ProfileFeature.DRIVE_REVERSE,
        ProfileFeature.DRIVE_SPEED_SETTING,
        ProfileFeature.SPOT_MOWING,
        ProfileFeature.UNDOCK,
        ProfileFeature.CUSTOM_PATHS,
        ProfileFeature.LIGHT,
        ProfileFeature.CAMERA,
        ProfileFeature.RTK,
        ProfileFeature.FIRMWARE_UPDATE,
        ProfileFeature.BASE_STATION_FIRMWARE_UPDATE,
        ProfileFeature.MAINTENANCE,
        ProfileReport.MAP_GEOMETRY,
        ProfileReport.SATELLITES,
        ProfileReport.BASE_STATION,
    }
)

_VARIES = frozenset(
    {
        ProfileFeature.START_PLANS,
        ProfileFeature.BLADE_HEIGHT,
        ProfileReport.POSITION,
        ProfileReport.BLADE_HEIGHT,
        ProfileReport.RAIN_SENSOR,
        mapping.RAIN_DELAY_SETTING,
    }
)


def representative_mower() -> WorxMower:
    """A fully equipped mower (every module, two zones, edge cut, rain delay)."""
    return WorxMower.from_item(
        product_item(
            "00000000000000000000", name="Landroid", model="Landroid", online=True, full=True
        )
    )


def capability_profiles() -> list[CapabilityProfile]:
    """The provider's model families (one)."""
    return [
        CapabilityProfile(
            family="landroid",
            name="Landroid, Kress and LandXcape",
            models=["Worx Landroid", "Kress", "LandXcape"],
            capabilities=mapping.capabilities_of(representative_mower()),
            reports=frozenset(
                {
                    ProfileReport.POSITION,
                    ProfileReport.BLADE_HEIGHT,
                    ProfileReport.SIGNAL,
                    ProfileReport.ERROR_CODES,
                    ProfileReport.LIFT_DETECTION,
                    ProfileReport.RAIN_SENSOR,
                    ProfileReport.BATTERY_CYCLES,
                    ProfileReport.FIRMWARE_VERSION,
                }
            ),
            varies=frozenset(str(key) for key in _VARIES),
            not_offered=frozenset(key.value for key in _NOT_OFFERED),
            notes=[
                {
                    "en": "Start plans: now, the zones set up in the app (up to four) and the "
                    "edge cut on mowers with the one-time schedule.",
                    "de": "Startpläne: jetzt, die in der App eingerichteten Zonen (bis zu vier) "
                    "und der Kantenschnitt bei Mähern mit dem einmaligen Zeitplan.",
                },
                {
                    "en": "Blade height only with the cutting height module, positions only with "
                    "a GPS module, the rain delay only where the mower reports one.",
                    "de": "Schnitthöhe nur mit dem Schnitthöhenmodul, Positionen nur mit einem "
                    "GPS-Modul, die Regenverzögerung nur, wo der Mäher eine meldet.",
                },
                {
                    "en": "The emergency stop is an immediate pause.",
                    "de": "Der Not-Halt ist eine sofortige Pause.",
                },
            ],
        )
    ]
