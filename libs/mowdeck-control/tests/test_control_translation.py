"""Neutral mowing request to native job."""

from typing import Any

import pytest
from mowdeck_contracts.v1 import MowerCapabilities, StartMow
from mowdeck_control import (
    MowOrder,
    MowRequest,
    NativeJob,
    Pattern,
    UnsupportedPattern,
    plan_to_native_job,
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
    assert defaults.order is MowOrder.BORDER_FIRST


def test_request_from_start_mow_errors() -> None:
    with pytest.raises(ValueError, match="Planner"):
        MowRequest.from_start_mow(StartMow(plan_ref="plan-1"))
    with pytest.raises(ValueError, match="invalid pattern settings"):
        MowRequest.from_start_mow(StartMow(area_ids=["a"], pattern={"order": "sideways"}))
    with pytest.raises(ValueError, match="invalid pattern settings"):
        MowRequest.from_start_mow(StartMow(area_ids=["a"], pattern={"angle_deg": "steep"}))
