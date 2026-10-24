"""MowPattern: the common pattern settings of a native start (and the Fleet mow defaults)."""

import pytest
from mowdeck_contracts.v1 import (
    NATIVE_PATTERN_NAMES,
    MowerCapabilities,
    MowPattern,
    ObstacleDetection,
    PathOrder,
    StartMow,
    StartOptions,
)
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


def test_mow_pattern_job_options_default_to_the_mowers_own() -> None:
    pattern = MowPattern()
    assert (
        pattern.obstacle_detection,
        pattern.obstacle_laps,
        pattern.order,
        pattern.start_progress_pct,
    ) == (None, None, None, None)
    # Unset options are not sent: the dump a client sends carries none of them.
    assert set(pattern.model_dump(exclude_none=True)) == {"name", "angle_deg", "border_laps"}


def test_mow_pattern_takes_the_job_options() -> None:
    pattern = MowPattern.model_validate(
        {
            "obstacle_detection": "sensitive",
            "obstacle_laps": 0,
            "order": "grid_first",
            "start_progress_pct": 40,
        }
    )
    assert pattern.obstacle_detection is ObstacleDetection.SENSITIVE
    assert pattern.order is PathOrder.GRID_FIRST
    assert (pattern.obstacle_laps, pattern.start_progress_pct) == (0, 40)


@pytest.mark.parametrize(
    "data",
    [
        {"obstacle_detection": "eager"},
        {"obstacle_laps": 5},
        {"obstacle_laps": -1},
        {"order": "random"},
        {"start_progress_pct": 100},
        {"start_progress_pct": -1},
    ],
)
def test_mow_pattern_rejects_job_options_out_of_range(data: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        MowPattern.model_validate(data)


LUBA = StartOptions(
    obstacle_detection=[
        ObstacleDetection.OFF,
        ObstacleDetection.STANDARD,
        ObstacleDetection.SENSITIVE,
    ],
    obstacle_laps_range=(0, 4),
    orders=[PathOrder.BORDER_FIRST, PathOrder.GRID_FIRST],
    start_progress_range_pct=(0, 99),
)


def test_start_options_accept_what_they_declare() -> None:
    pattern = MowPattern(
        obstacle_detection=ObstacleDetection.SENSITIVE,
        obstacle_laps=2,
        order=PathOrder.BORDER_FIRST,
        start_progress_pct=99,
    )
    assert LUBA.problem(pattern) is None
    assert StartOptions().problem(MowPattern()) is None
    # Progress 0 is the beginning: every mower can start there.
    assert StartOptions().problem(MowPattern(start_progress_pct=0)) is None


@pytest.mark.parametrize(
    ("options", "pattern", "needle"),
    [
        (LUBA, {"obstacle_detection": "slow_touch"}, "obstacle detection"),
        (StartOptions(), {"obstacle_detection": "standard"}, "obstacle detection"),
        (StartOptions(), {"obstacle_laps": 1}, "no-go zones"),
        (StartOptions(obstacle_laps_range=(1, 2)), {"obstacle_laps": 3}, "between 1 and 2"),
        (StartOptions(), {"order": "grid_first"}, "path order"),
        (StartOptions(orders=[PathOrder.GRID_FIRST]), {"order": "border_first"}, "path order"),
        (StartOptions(), {"start_progress_pct": 10}, "start progress"),
        (StartOptions(start_progress_range_pct=(0, 50)), {"start_progress_pct": 60}, "0 and 50"),
    ],
)
def test_start_options_refuse_what_they_do_not_declare(
    options: StartOptions, pattern: dict[str, object], needle: str
) -> None:
    problem = options.problem(MowPattern.model_validate(pattern))
    assert problem is not None
    assert needle in problem


@pytest.mark.parametrize(
    "data",
    [
        {"obstacle_detection": ["off", "off"]},
        {"orders": ["grid_first", "grid_first"]},
        {"obstacle_laps_range": [3, 1]},
        {"obstacle_laps_range": [0, 5]},
        {"start_progress_range_pct": [0, 100]},
        {"start_progress_range_pct": [-1, 10]},
    ],
)
def test_start_options_reject_malformed_declarations(data: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        StartOptions.model_validate(data)


def test_capabilities_declare_no_start_options_by_default() -> None:
    caps = MowerCapabilities()
    assert caps.start_options == StartOptions()
    assert caps.model_dump(mode="json")["start_options"] == {
        "obstacle_detection": [],
        "obstacle_laps_range": None,
        "orders": [],
        "start_progress_range_pct": None,
    }
    assert MowerCapabilities(start_options=LUBA).start_options.orders == [
        PathOrder.BORDER_FIRST,
        PathOrder.GRID_FIRST,
    ]
