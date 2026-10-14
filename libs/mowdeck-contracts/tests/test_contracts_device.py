"""Device snapshots (map, capabilities), their subjects and the DEVICE stream."""

from datetime import UTC, datetime
from uuid import UUID

import pytest
from mowdeck_contracts.v1 import (
    GeoReference,
    MapArea,
    MowerCapabilities,
    MowerCapabilitiesSnapshot,
    MowerMap,
    MowerMapSnapshot,
    UiAreaDef,
)
from mowdeck_contracts.v1.subjects import (
    ALL_DEVICE_SNAPSHOTS,
    DEVICE,
    device_subject,
    parse_device_subject,
    stream_for_subject,
)
from pydantic import ValidationError

MOWER = UUID("6f1c2b8e-3d4a-4f5b-8c9d-0e1f2a3b4c5d")
ACCOUNT = UUID("0b8f7a52-1111-4a0e-9a59-2f3c4d5e6f70")


def test_device_subjects_round_trip() -> None:
    assert device_subject(MOWER, "map") == f"device.mower.{MOWER}.map"
    assert device_subject(MOWER, "capabilities") == f"device.mower.{MOWER}.capabilities"
    assert parse_device_subject(device_subject(MOWER, "map")) == (MOWER, "map")
    assert stream_for_subject(device_subject(MOWER, "capabilities")) is DEVICE
    assert stream_for_subject(ALL_DEVICE_SNAPSHOTS) is DEVICE
    assert DEVICE.max_msgs_per_subject == 1


@pytest.mark.parametrize(
    "subject",
    [f"device.mower.{MOWER}.photo", "device.mower.x", f"telemetry.mower.{MOWER}", "device"],
)
def test_parse_device_subject_rejects_other_subjects(subject: str) -> None:
    with pytest.raises(ValueError, match="device snapshot"):
        parse_device_subject(subject)


def test_device_subject_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="kind"):
        device_subject(MOWER, "photo")  # type: ignore[arg-type]


def test_snapshots_round_trip_as_json() -> None:
    at = datetime(2026, 10, 9, 12, tzinfo=UTC)
    mower_map = MowerMap(
        reference=GeoReference(origin_lat=48.0, origin_lon=11.0),
        areas=[MapArea(id="front", name="Front", boundary=[(0, 0), (1, 0), (1, 1)])],
        revision="r1",
    )
    snap = MowerMapSnapshot(mower_id=MOWER, account_id=ACCOUNT, observed_at=at, map=mower_map)
    assert MowerMapSnapshot.model_validate_json(snap.model_dump_json()) == snap
    caps = MowerCapabilitiesSnapshot(
        mower_id=MOWER,
        account_id=ACCOUNT,
        observed_at=at,
        capabilities=MowerCapabilities(blade_height_range_mm=(20, 100), max_speed_mps=1.0),
    )
    again = MowerCapabilitiesSnapshot.model_validate_json(caps.model_dump_json())
    assert again.capabilities.blade_height_range_mm == (20, 100)


def test_ui_area_labels_are_optional_and_checked() -> None:
    area = UiAreaDef(route="/mowers", menu_label="Mowers", required_permission="mower.view")
    assert area.labels == {}
    translated = UiAreaDef(
        route="/mowers",
        menu_label="Mowers",
        required_permission="mower.view",
        labels={"de": "Mäher", "en-GB": "Mowers"},
    )
    assert translated.labels["de"] == "Mäher"
    with pytest.raises(ValidationError):
        UiAreaDef(route="/m", menu_label="M", required_permission="m.v", labels={"German": "Mäher"})
