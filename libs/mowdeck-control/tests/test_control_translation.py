"""Neutral mowing request to native job."""

from typing import Any

import pytest
from mowdeck_contracts.v1 import (
    MowerCapabilities,
    ObstacleDetection,
    PathOrder,
    StartMow,
    StartOptions,
)
from mowdeck_control import (
    MowOrder,
    MowRequest,
    NativeJob,
    Pattern,
    UnsupportedPattern,
    plan_to_native_job,
    without_undeclared_options,
)

CAPS = MowerCapabilities(
    native_patterns=frozenset({"stripes", "diagonal", "checkerboard", "edge_only"})
)


@pytest.mark.parametrize(
    ("pattern", "angle", "passes"),
    [
        (Pattern.STRIPES, 30.0, (30.0,)),
        (Pattern.STRIPES, 200.0, (20.0,)),
        (Pattern.DIAGONAL, 0.0, (45.0,)),
        (Pattern.DIAGONAL, 150.0, (15.0,)),
        (Pattern.CHECKERBOARD, 10.0, (10.0, 100.0)),
        (Pattern.EDGE_ONLY, 0.0, ()),
    ],
)
def test_supported_patterns(pattern: str, angle: float, passes: tuple[float, ...]) -> None:
    job = plan_to_native_job(MowRequest(area_ids=("a",), pattern=pattern, angle_deg=angle), CAPS)
    assert isinstance(job, NativeJob)
    assert job.passes == pytest.approx(passes)
    assert job.area_ids == ("a",)
    assert job.pattern == pattern
    assert job.border_laps == (1 if pattern == Pattern.EDGE_ONLY else 0)


def test_unsupported_pattern() -> None:
    caps = MowerCapabilities(native_patterns=frozenset({"stripes"}))
    with pytest.raises(UnsupportedPattern, match="spiral"):
        plan_to_native_job(MowRequest(area_ids=("a",), pattern="spiral"), caps)
    with pytest.raises(UnsupportedPattern, match="checkerboard"):
        plan_to_native_job(MowRequest(area_ids=("a",), pattern="checkerboard"), caps)
    with pytest.raises(UnsupportedPattern):
        plan_to_native_job(MowRequest(area_ids=("a",)), MowerCapabilities())


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"area_ids": ()}, "area id"),
        ({"spacing_m": 0.01}, "spacing_m"),
        ({"spacing_m": float("nan")}, "spacing_m"),
        ({"border_laps": 5}, "border_laps"),
        ({"angle_deg": float("inf")}, "angle_deg"),
    ],
)
def test_invalid_settings(overrides: dict[str, Any], message: str) -> None:
    data: dict[str, Any] = {"area_ids": ("a",), **overrides}
    with pytest.raises(ValueError, match=message):
        plan_to_native_job(MowRequest(**data), CAPS)


def test_request_from_start_mow() -> None:
    command = StartMow(
        area_ids=["a", "b"],
        pattern={
            "name": "checkerboard",
            "angle_deg": 15,
            "spacing_m": 0.3,
            "border_laps": 2,
            "order": "grid_first",
        },
    )
    request = MowRequest.from_start_mow(command)
    assert request == MowRequest(
        area_ids=("a", "b"),
        pattern="checkerboard",
        angle_deg=15.0,
        spacing_m=0.3,
        border_laps=2,
        order=MowOrder.GRID_FIRST,
    )
    defaults = MowRequest.from_start_mow(StartMow(area_ids=["a"]))
    assert defaults.pattern == Pattern.STRIPES
    assert defaults.order is None
    assert (defaults.obstacle_detection, defaults.obstacle_laps, defaults.start_progress_pct) == (
        None,
        None,
        None,
    )


def test_request_from_start_mow_errors() -> None:
    with pytest.raises(ValueError, match="Planner"):
        MowRequest.from_start_mow(StartMow(plan_ref="plan-1"))
    with pytest.raises(ValueError, match="invalid pattern settings"):
        MowRequest.from_start_mow(StartMow(area_ids=["a"], pattern={"order": "sideways"}))
    with pytest.raises(ValueError, match="invalid pattern settings"):
        MowRequest.from_start_mow(StartMow(area_ids=["a"], pattern={"angle_deg": "steep"}))


LUBA_OPTIONS = StartOptions(
    obstacle_detection=[
        ObstacleDetection.OFF,
        ObstacleDetection.STANDARD,
        ObstacleDetection.SENSITIVE,
    ],
    obstacle_laps_range=(0, 4),
    orders=[PathOrder.BORDER_FIRST, PathOrder.GRID_FIRST],
    start_progress_range_pct=(0, 99),
)
WITH_OPTIONS = CAPS.model_copy(update={"start_options": LUBA_OPTIONS})


def test_job_options_pass_through_when_declared() -> None:
    command = StartMow(
        area_ids=["a"],
        pattern={
            "obstacle_detection": "sensitive",
            "obstacle_laps": 0,
            "order": "grid_first",
            "start_progress_pct": 30,
        },
    )
    job = plan_to_native_job(MowRequest.from_start_mow(command), WITH_OPTIONS)
    assert job.obstacle_detection is ObstacleDetection.SENSITIVE
    assert job.order is MowOrder.GRID_FIRST
    assert (job.obstacle_laps, job.start_progress_pct) == (0, 30)


def test_job_options_left_out_stay_the_mowers_own() -> None:
    job = plan_to_native_job(MowRequest.from_start_mow(StartMow(area_ids=["a"])), WITH_OPTIONS)
    assert (job.obstacle_detection, job.obstacle_laps, job.order, job.start_progress_pct) == (
        None,
        None,
        None,
        None,
    )
    # A start at 0 % is the beginning, the same as no start progress (any mower runs it).
    zero = MowRequest.from_start_mow(StartMow(area_ids=["a"], pattern={"start_progress_pct": 0}))
    assert plan_to_native_job(zero, CAPS).start_progress_pct is None


@pytest.mark.parametrize(
    ("pattern", "message"),
    [
        ({"obstacle_detection": "standard"}, "obstacle detection"),
        ({"obstacle_laps": 2}, "no-go zones"),
        ({"order": "border_first"}, "path order"),
        ({"start_progress_pct": 50}, "start progress"),
    ],
)
def test_job_options_need_the_capability(pattern: dict[str, Any], message: str) -> None:
    request = MowRequest.from_start_mow(StartMow(area_ids=["a"], pattern=pattern))
    with pytest.raises(ValueError, match=message):
        plan_to_native_job(request, CAPS)


@pytest.mark.parametrize(
    "pattern",
    [
        {"obstacle_detection": "eager"},
        {"obstacle_laps": 7},
        {"start_progress_pct": 100},
    ],
)
def test_job_options_out_of_range_are_invalid_settings(pattern: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="invalid pattern settings"):
        MowRequest.from_start_mow(StartMow(area_ids=["a"], pattern=pattern))


def test_undeclared_job_options_are_left_to_the_mower() -> None:
    job = {"name": "stripes", "border_laps": 1, "order": "border_first", "obstacle_laps": 2}
    assert without_undeclared_options(job, CAPS) == {"name": "stripes", "border_laps": 1}
    assert without_undeclared_options(job, WITH_OPTIONS) == job
    only_grid = CAPS.model_copy(
        update={"start_options": StartOptions(orders=[PathOrder.GRID_FIRST])}
    )
    assert "order" not in without_undeclared_options(job, only_grid)
    # Malformed values stay for the start to refuse; unset ones are untouched.
    assert without_undeclared_options({"order": "sideways", "obstacle_laps": None}, CAPS) == {
        "order": "sideways",
        "obstacle_laps": None,
    }
