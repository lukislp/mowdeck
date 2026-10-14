"""MowPattern: the common pattern settings of a native start (and the Fleet mow defaults)."""

import pytest
from mowdeck_contracts.v1 import NATIVE_PATTERN_NAMES, MowPattern, StartMow
from pydantic import ValidationError


def test_mow_pattern_defaults_mow_the_edge_once_in_stripes() -> None:
    pattern = MowPattern()
    assert (pattern.name, pattern.angle_deg, pattern.border_laps, pattern.spacing_m) == (
        "stripes",
        0.0,
        1,
        None,
    )


def test_mow_pattern_dump_fits_start_mow() -> None:
    pattern = MowPattern(name="diagonal", angle_deg=45, border_laps=2, spacing_m=0.3)
    command = StartMow(area_ids=["1"], pattern=pattern.model_dump(exclude_none=True))
    assert command.pattern == {
        "name": "diagonal",
        "angle_deg": 45.0,
        "border_laps": 2,
        "spacing_m": 0.3,
    }


@pytest.mark.parametrize("name", NATIVE_PATTERN_NAMES)
def test_mow_pattern_accepts_every_native_name(name: str) -> None:
    assert MowPattern(name=name).name == name  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "data",
    [
        {"name": "spiral"},
        {"angle_deg": -1},
        {"angle_deg": 181},
        {"border_laps": 5},
        {"border_laps": -1},
        {"spacing_m": 0.01},
        {"spacing_m": 3},
    ],
)
def test_mow_pattern_rejects_out_of_range(data: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        MowPattern.model_validate(data)
