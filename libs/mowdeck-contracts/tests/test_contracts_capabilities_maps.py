"""MowerCapabilities and MowerMap."""

import json
from typing import Any

import pytest
from mowdeck_contracts.v1 import (
    CameraAccess,
    ConnectionKind,
    GeoReference,
    MapArea,
    MowerCapabilities,
    MowerMap,
    RtkSource,
)
from pydantic import ValidationError

SQUARE = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]


def _map(**overrides: Any) -> MowerMap:
    data: dict[str, Any] = {
        "reference": {"origin_lat": 48.1, "origin_lon": 11.5, "rotation_deg": 15},
        "areas": [{"id": "front", "name": "Front", "boundary": SQUARE}],
        "no_go_zones": [{"id": "bed", "boundary": [(1, 1), (2, 1), (2, 2)]}],
        "obstacles": [{"id": "tree", "boundary": [(5, 5), (6, 5), (6, 6)]}],
        "paths": [{"id": "p1", "points": [(0, 0), (0, -3)]}],
        "dock": {"x": -1, "y": 0.5, "heading_deg": 90},
        "revision": "abc",
        **overrides,
    }
    return MowerMap.model_validate(data)


def test_capabilities_defaults_support_nothing() -> None:
    caps = MowerCapabilities()
    assert caps.native_patterns == frozenset()
    assert not caps.manual_drive
    assert not caps.manual_drive_blades
    assert not caps.custom_paths
    assert caps.camera is CameraAccess.NONE
    assert not caps.light
    assert not caps.supports_pattern("stripes")
    assert not caps.blade_height_allowed(50)
    assert not caps.speed_allowed(0.5)


def test_capabilities_checks_and_deterministic_dump() -> None:
    caps = MowerCapabilities(
        model="LUBA 2 AWD 5000X",
        native_patterns=frozenset({"stripes", "checkerboard", "diagonal"}),
        manual_drive=True,
        camera=CameraAccess.CLOUD,
        rtk_sources=frozenset({RtkSource.NETWORK, RtkSource.BASE_STATION}),
        blade_height_range_mm=(25, 70),
        max_speed_mps=1.0,
        connection_kinds=frozenset({ConnectionKind.CLOUD, ConnectionKind.BLE_BRIDGE}),
    )
    assert caps.supports_pattern("checkerboard")
    assert caps.blade_height_allowed(25)
    assert caps.blade_height_allowed(70)
    assert not caps.blade_height_allowed(71)
    assert caps.speed_allowed(1.0)
    assert not caps.speed_allowed(1.01)
    assert not caps.speed_allowed(0)
    dumped = json.loads(caps.model_dump_json())
    assert dumped["native_patterns"] == ["checkerboard", "diagonal", "stripes"]
    assert dumped["rtk_sources"] == ["base_station", "network"]
    assert dumped["connection_kinds"] == ["ble_bridge", "cloud"]
    assert MowerCapabilities.model_validate_json(caps.model_dump_json()) == caps


@pytest.mark.parametrize("value", [(70, 25), (-1, 10)])
def test_capabilities_reject_bad_blade_range(value: tuple[int, int]) -> None:
    with pytest.raises(ValidationError, match="blade_height_range_mm"):
        MowerCapabilities(blade_height_range_mm=value)


def test_map_round_trip_and_lookup() -> None:
    mower_map = _map()
    assert mower_map.area_ids == ["front"]
    area = mower_map.area("front")
    assert isinstance(area, MapArea)
    assert area.boundary[2] == (10.0, 10.0)
    assert mower_map.area("missing") is None
    assert [z.id for z in mower_map.forbidden_zones()] == ["bed", "tree"]
    assert MowerMap.model_validate_json(mower_map.model_dump_json()) == mower_map


def test_map_minimal() -> None:
    mower_map = MowerMap(reference=GeoReference(origin_lat=0, origin_lon=0))
    assert mower_map.areas == []
    assert mower_map.dock is None
    assert mower_map.reference.rotation_deg == 0


def test_map_rejects_duplicate_ids() -> None:
    with pytest.raises(ValidationError, match="duplicates: front"):
        _map(no_go_zones=[{"id": "front", "boundary": SQUARE}])


@pytest.mark.parametrize(
    "overrides",
    [
        {"areas": [{"id": "a", "boundary": [(0, 0), (1, 1)]}]},
        {"areas": [{"id": "", "boundary": SQUARE}]},
        {"paths": [{"id": "p", "points": [(0, 0)]}]},
        {"dock": {"x": 0, "y": 0, "heading_deg": 360}},
        {"reference": {"origin_lat": 91, "origin_lon": 0}},
    ],
)
def test_map_rejects_invalid_geometry(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _map(**overrides)


def test_capabilities_cutting_width_is_optional_and_positive() -> None:
    assert MowerCapabilities().cutting_width_mm is None
    assert MowerCapabilities(cutting_width_mm=400).cutting_width_mm == 400
    for bad in (0, -1, 2001):
        with pytest.raises(ValidationError):
            MowerCapabilities(cutting_width_mm=bad)


def test_capabilities_speed_range_respects_the_minimum() -> None:
    caps = MowerCapabilities(min_speed_mps=0.2, max_speed_mps=0.6)
    assert caps.speed_allowed(0.2)
    assert caps.speed_allowed(0.6)
    assert not caps.speed_allowed(0.19)
    assert not caps.speed_allowed(0.61)
    only_min = MowerCapabilities(min_speed_mps=0.2)
    assert not only_min.speed_allowed(0.3)  # no settable speed without a maximum
    assert MowerCapabilities(min_speed_mps=0.4, max_speed_mps=0.4).speed_allowed(0.4)


@pytest.mark.parametrize(
    ("low", "high", "match"),
    [
        (0.7, 0.6, "min_speed_mps must not exceed"),
        (0.0, 0.6, "min_speed_mps"),
        (5.5, None, "min_speed_mps"),
    ],
)
def test_capabilities_reject_bad_speed_range(low: float, high: float | None, match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        MowerCapabilities(min_speed_mps=low, max_speed_mps=high)


def test_capabilities_path_spacing_range_is_optional_and_ordered() -> None:
    assert MowerCapabilities().path_spacing_range_mm is None
    caps = MowerCapabilities(path_spacing_range_mm=(200, 350))
    assert json.loads(caps.model_dump_json())["path_spacing_range_mm"] == [200, 350]
    for bad in [(350, 200), (0, 100)]:
        with pytest.raises(ValidationError, match="path_spacing_range_mm"):
            MowerCapabilities(path_spacing_range_mm=bad)
