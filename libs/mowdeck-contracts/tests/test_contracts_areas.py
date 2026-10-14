"""The display name rule of map areas and the area names event."""

from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import (
    AREA_NAME_MAX_LENGTH,
    FLEET_AREA_NAMES_CHANGED,
    AreaNamesChanged,
    EventEnvelope,
    GeoReference,
    MapArea,
    MowerMap,
    area_display_name,
    area_display_names,
    area_fallback_name,
    clean_area_name,
    is_placeholder_name,
)
from mowdeck_contracts.v1.subjects import event_subject_for_type

SQUARE = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)]


def mower_map(*names: str | None) -> MowerMap:
    return MowerMap(
        reference=GeoReference(origin_lat=0, origin_lon=0),
        areas=[MapArea(id=f"a{i}", name=name, boundary=SQUARE) for i, name in enumerate(names)],
    )


def test_custom_name_wins_over_the_mower_name_and_the_fallback() -> None:
    plan = mower_map("Front", None)
    assert area_display_name("a0", plan, {"a0": "Vorgarten"}) == "Vorgarten"
    assert area_display_name("a1", plan, {"a1": "  Hinten  "}) == "Hinten"
    # A blank override counts as none.
    assert area_display_name("a0", plan, {"a0": "  "}) == "Front"


def test_the_mower_name_is_used_unless_it_is_a_placeholder() -> None:
    plan = mower_map(" Front lawn ", "4525245168006937683", "", None, "2")
    names = area_display_names(plan)
    assert names == {
        "a0": "Front lawn",
        "a1": "Area 2",
        "a2": "Area 3",
        "a3": "Area 4",
        "a4": "2",  # a short number is a real name
    }


def test_fallback_numbers_follow_the_map_order_and_the_locale() -> None:
    plan = mower_map(None, "Named", None)
    # The number is the position among all areas: naming one area renumbers nothing.
    assert area_display_name("a0", plan, locale="de") == "Bereich 1"
    assert area_display_name("a2", plan, locale="de-DE") == "Bereich 3"
    assert area_display_name("a2", plan.areas, {"a1": "x"}, locale="fr") == "Area 3"
    assert area_fallback_name(0, "de_AT") == "Bereich 1"
    assert area_fallback_name(4) == "Area 5"


def test_an_area_missing_from_the_map_keeps_its_override_or_id() -> None:
    assert area_display_name("gone", mower_map("A"), {"gone": "Alt"}) == "Alt"
    assert area_display_name("gone", mower_map("A")) == "gone"
    assert area_display_name("gone", None) == "gone"
    assert area_display_names(None) == {}


def test_placeholder_and_cleaning() -> None:
    assert is_placeholder_name(None)
    assert is_placeholder_name(" 12345678 ")
    assert not is_placeholder_name("1234567")
    assert clean_area_name("  Front \n  lawn ") == "Front lawn"
    assert clean_area_name("   ") is None
    assert clean_area_name(None) is None
    assert clean_area_name("x" * AREA_NAME_MAX_LENGTH) == "x" * AREA_NAME_MAX_LENGTH
    with pytest.raises(ValueError, match="at most"):
        clean_area_name("x" * (AREA_NAME_MAX_LENGTH + 1))


def test_area_names_changed_event_round_trips() -> None:
    payload = AreaNamesChanged(mower_id=uuid4(), account_id=uuid4(), names={"a1": "Front"})
    envelope = EventEnvelope[AreaNamesChanged](
        type=FLEET_AREA_NAMES_CHANGED,
        account_id=payload.account_id,
        producer="fleet",
        payload=payload,
    )
    parsed = EventEnvelope[AreaNamesChanged].model_validate_json(envelope.model_dump_json())
    assert parsed.payload == payload
    assert event_subject_for_type(FLEET_AREA_NAMES_CHANGED) == (
        "event.fleet.mower.area_names_changed"
    )
