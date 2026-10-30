"""Capability profiles: features derived from capabilities, consistent declarations (ADR 0071)."""

import pytest
from mowdeck_contracts.v1 import (
    PROFILE_KEYS,
    SETTING_KEYS,
    CameraAccess,
    CapabilityProfile,
    ConnectionKind,
    MowerCapabilities,
    ProfileFeature,
    ProfileReport,
    RtkSource,
    SettingGroup,
    SettingSpec,
    SettingType,
    StartOptions,
    StartPlan,
    StartPlanKind,
    check_profiles,
    profile_features,
)
from pydantic import ValidationError


def test_capability_profiles_minimal_capabilities_offer_nothing() -> None:
    assert profile_features(MowerCapabilities()) == frozenset()


def test_capability_profiles_derive_every_feature() -> None:
    caps = MowerCapabilities(
        connection_kinds=frozenset({ConnectionKind.CLOUD, ConnectionKind.WIFI}),
        links_supported=["ble_bridge"],
        native_patterns=frozenset({"stripes"}),
        start_options=StartOptions(obstacle_laps_range=(0, 4)),
        start_plans=[StartPlan(ref="now", kind=StartPlanKind.NOW)],
        dock_keeps_job=True,
        blade_height_range_mm=(20, 60),
        path_spacing_range_mm=(100, 300),
        max_speed_mps=0.6,
        manual_drive=True,
        manual_drive_blades=True,
        drive_reverse=True,
        undock=True,
        custom_paths=True,
        light=True,
        camera=CameraAccess.LOCAL,
        rtk_sources=frozenset({RtkSource.NETWORK}),
        firmware_update=True,
        base_station_firmware_update=True,
        maintenance=frozenset({"sync_map"}),
    )
    missing = set(ProfileFeature) - profile_features(caps)
    assert missing == {ProfileFeature.DRIVE_SPEED_SETTING}


def test_capability_profiles_keys_are_distinct_and_cover_settings() -> None:
    features = {f.value for f in ProfileFeature}
    reports = {r.value for r in ProfileReport}
    assert not features & reports
    assert not (features | reports) & set(SETTING_KEYS)
    assert features | reports | set(SETTING_KEYS) == PROFILE_KEYS


def _profile(**update: object) -> CapabilityProfile:
    values: dict[str, object] = {
        "family": "rover",
        "name": "Rover",
        "capabilities": MowerCapabilities(
            connection_kinds=frozenset({ConnectionKind.CLOUD}),
            settings=[
                SettingSpec(key="rain_detection", type=SettingType.BOOL, group=SettingGroup.WEATHER)
            ],
        ),
        "reports": frozenset({ProfileReport.POSITION}),
    }
    values.update(update)
    return CapabilityProfile.model_validate(values)


def test_capability_profiles_supported_keys() -> None:
    assert _profile().supported == {"transport_cloud", "position", "rain_detection"}


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ({"varies": frozenset({"warp_drive"})}, "unknown profile keys: warp_drive"),
        (
            {"varies": frozenset({"camera"}), "not_offered": frozenset({"camera"})},
            "cannot both vary",
        ),
        ({"not_offered": frozenset({"position"})}, "marked not offered are supported"),
        ({"not_offered": frozenset({"rain_detection"})}, "marked not offered are supported"),
        ({"models": ["A", "A"]}, "list a model twice"),
        ({"family": "Rover"}, "family"),
    ],
)
def test_capability_profiles_refuse_inconsistent_declarations(
    update: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        _profile(**update)


def test_capability_profiles_serialise_sorted() -> None:
    profile = _profile(
        reports=frozenset({ProfileReport.SIGNAL, ProfileReport.POSITION}),
        varies=frozenset({"light", "camera"}),
    )
    dumped = profile.model_dump(mode="json")
    assert dumped["reports"] == ["position", "signal"]
    assert dumped["varies"] == ["camera", "light"]
    assert CapabilityProfile.model_validate(dumped) == profile


def test_capability_profiles_check_duplicate_families() -> None:
    assert check_profiles([_profile(), _profile(family="other")]) == []
    assert check_profiles([_profile(), _profile()]) == ["family 'rover' is declared twice"]
