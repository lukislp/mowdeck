"""The Husqvarna capability profile (ADR 0071)."""

from mowdeck_contracts.v1 import ProfileFeature, ProfileReport
from mowdeck_control_husqvarna import mapping
from mowdeck_control_husqvarna.plugin import SPEC


def test_husqvarna_profile_is_the_connection_capabilities() -> None:
    assert SPEC.capability_profiles is not None
    (profile,) = SPEC.capability_profiles()
    assert profile.capabilities == mapping.capabilities_of(None)
    assert not profile.tested_on_hardware
    assert profile.supported == {
        ProfileFeature.TRANSPORT_CLOUD.value,
        ProfileReport.POSITION.value,
        ProfileReport.ERROR_CODES.value,
        ProfileReport.LIFT_DETECTION.value,
        ProfileReport.BATTERY_CYCLES.value,
    }
    assert ProfileFeature.MANUAL_DRIVE.value in profile.not_offered
    assert ProfileReport.MAP_GEOMETRY.value in profile.not_offered
