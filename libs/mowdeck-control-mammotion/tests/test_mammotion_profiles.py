"""The Mammotion capability profiles (ADR 0071)."""

from mowdeck_contracts.v1 import ProfileFeature, ProfileReport, check_profiles
from mowdeck_control.providers import installed_specs
from mowdeck_control_mammotion import profiles
from mowdeck_control_mammotion.port import DeviceType
from mowdeck_control_mammotion.provider import SPEC


def test_mammotion_profiles_are_registered_with_the_spec() -> None:
    assert SPEC.capability_profiles is not None
    assert installed_specs()["mammotion"].capability_profiles is SPEC.capability_profiles
    found = SPEC.capability_profiles()
    assert [p.family for p in found] == [f.family for f in profiles.FAMILIES]
    assert check_profiles(found) == []


def test_mammotion_profile_device_names_belong_to_their_family() -> None:
    for family in profiles.FAMILIES:
        assert DeviceType.value_of_str(family.device_name, "") is family.types[0]
        for name in family.device_names:
            assert DeviceType.value_of_str(name, "") in family.types, (family.family, name)


def test_mammotion_families_do_not_overlap() -> None:
    seen: set[DeviceType] = set()
    for family in profiles.FAMILIES:
        assert not seen & set(family.types), family.family
        seen |= set(family.types)


def test_mammotion_profiles_follow_the_capability_code() -> None:
    by_family = {p.family: p for p in profiles.capability_profiles()}
    luba_1, awd_x = by_family["luba_1"], by_family["luba_2_awd_x"]
    assert awd_x.tested_on_hardware and not luba_1.tested_on_hardware
    assert sum(p.tested_on_hardware for p in by_family.values()) == 1
    # The LUBA 1 has no camera, no light and no settings; the AWD X has all three.
    assert {ProfileFeature.CAMERA.value, ProfileFeature.LIGHT.value} <= luba_1.not_offered
    assert not luba_1.capabilities.settings
    assert ProfileFeature.CAMERA.value in awd_x.supported
    assert ProfileFeature.LIGHT.value in awd_x.supported
    assert "blade_speed" in awd_x.supported
    assert awd_x.capabilities.blade_height_range_mm == (25, 70)
    # Every family is reached through the cloud and the Bluetooth bridge, never locally.
    for profile in by_family.values():
        assert {"transport_cloud", "transport_ble_bridge"} <= profile.supported
        assert ProfileFeature.TRANSPORT_WIFI.value in profile.not_offered
        assert ProfileReport.MAP_GEOMETRY in profile.reports


def test_mammotion_keys_that_differ_inside_a_family_vary() -> None:
    yuka_mini = next(p for p in profiles.capability_profiles() if p.family == "yuka_mini")
    # The Yuka MV has no night light (ADR 0046 amendment); the other minis do.
    assert "night_light" in yuka_mini.varies
