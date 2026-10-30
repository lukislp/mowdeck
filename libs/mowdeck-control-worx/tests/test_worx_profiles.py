"""The Worx capability profile (ADR 0071)."""

from mowdeck_contracts.v1 import ProfileFeature, StartPlanKind
from mowdeck_control_worx import mapping, profiles
from mowdeck_control_worx.plugin import SPEC


def test_worx_profile_is_a_fully_equipped_mower() -> None:
    assert SPEC.capability_profiles is not None
    (profile,) = SPEC.capability_profiles()
    assert profile.capabilities == mapping.capabilities_of(profiles.representative_mower())
    kinds = [plan.kind for plan in profile.capabilities.start_plans]
    assert kinds == [StartPlanKind.NOW, StartPlanKind.ZONE, StartPlanKind.ZONE, StartPlanKind.EDGE]
    assert profile.capabilities.blade_height_range_mm == mapping.BLADE_HEIGHT_RANGE_MM
    assert {ProfileFeature.START_PLANS.value, ProfileFeature.BLADE_HEIGHT.value} <= profile.varies
    assert mapping.RAIN_DELAY_SETTING in profile.supported
    assert ProfileFeature.DOCK_KEEPS_JOB.value in profile.not_offered
    assert not profile.tested_on_hardware
