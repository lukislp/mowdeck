"""The capability profile of the Husqvarna provider (ADR 0071).

One family: every mower of the Automower Connect API answers with the same capabilities
(:func:`~mowdeck_control_husqvarna.mapping.capabilities_of`), only the model name and the start
plans differ. The profile shows the start plans of a model with one work area: mow now for a
duration, the work area and back to the schedule; work areas exist only on models with that API
capability, so the start plans are listed as ``varies``. The API's other per-model capability
flags (positions, headlights, stay-out zones) change what a mower reports, not what MowDeck
offers on it; they are listed as ``varies`` too.

``not_offered`` names what the Automower Connect API does not offer (ADR 0066 research):
pattern parameters, manual driving, a camera, RTK details, map geometry, firmware updates and
maintenance commands. Everything else MowDeck does not offer is missing in MowDeck itself (for
example the cutting height, which the API sets as a level from 1 to 9, not in millimetres).
"""

from mowdeck_contracts.v1 import (
    CapabilityProfile,
    MowerCapabilities,
    ProfileFeature,
    ProfileReport,
    StartPlan,
    StartPlanKind,
)

from mowdeck_control_husqvarna import mapping

_NOT_OFFERED = frozenset(
    {
        ProfileFeature.TRANSPORT_WIFI,
        ProfileFeature.NATIVE_PATTERNS,
        ProfileFeature.START_OPTIONS,
        ProfileFeature.PATH_SPACING,
        ProfileFeature.MOWING_SPEED,
        ProfileFeature.MANUAL_DRIVE,
        ProfileFeature.MANUAL_DRIVE_BLADES,
        ProfileFeature.DRIVE_REVERSE,
        ProfileFeature.DRIVE_SPEED_SETTING,
        ProfileFeature.SPOT_MOWING,
        ProfileFeature.UNDOCK,
        ProfileFeature.CUSTOM_PATHS,
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


def _representative_capabilities() -> MowerCapabilities:
    """The connection's capabilities with the start plans of a model with one work area."""
    plans = [
        StartPlan(ref=mapping.PLAN_DURATION, kind=StartPlanKind.NOW),
        StartPlan(ref=f"{mapping.PLAN_WORK_AREA}:1", kind=StartPlanKind.ZONE),
        StartPlan(ref=mapping.PLAN_SCHEDULE, kind=StartPlanKind.SCHEDULE),
    ]
    return mapping.capabilities_of(None).model_copy(update={"start_plans": plans})


def capability_profiles() -> list[CapabilityProfile]:
    """The provider's model families (one)."""
    return [
        CapabilityProfile(
            family="automower",
            name="Automower and Gardena smart",
            models=["Husqvarna Automower (Automower Connect)", "Gardena smart SILENO"],
            capabilities=_representative_capabilities(),
            reports=frozenset(
                {
                    ProfileReport.POSITION,
                    ProfileReport.ERROR_CODES,
                    ProfileReport.LIFT_DETECTION,
                    ProfileReport.BATTERY_CYCLES,
                }
            ),
            varies=frozenset({ProfileReport.POSITION.value, ProfileFeature.START_PLANS.value}),
            not_offered=frozenset(key.value for key in _NOT_OFFERED),
            notes=[
                {
                    "en": "Positions only on models with GPS (the API's position capability).",
                    "de": "Positionen nur bei Modellen mit GPS (Positions-Fähigkeit der API).",
                },
                {
                    "en": "The emergency stop pauses the mower at once: the API has no hard stop.",
                    "de": "Der Not-Halt pausiert den Mäher sofort: Die API kennt keinen harten "
                    "Stopp.",
                },
            ],
        )
    ]
