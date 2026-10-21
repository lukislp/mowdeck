"""Mowing pattern planning (libs/mowdeck-patterns), the Planner's computation.

A user draws the spot polygon (and holes) and picks a pattern chain with settings in the app;
the Planner turns it into the route the mower drives. The fuzz input is a small polygon in
local metres, a chain of one or two patterns and JSON settings. Properties, following
``planner.application.planning.Planner.compute``:

* ``Area.of`` and ``plan`` raise nothing but ``PatternError`` (the Planner answers 422 for
  those; anything else is a 500);
* an accepted plan converts to the ``SpotMowPlan`` contract and back, its GeoJSON preview
  builds, and every segment keeps a finite, positive speed.
"""

import json
import math
import sys
from typing import Any

import atheris

with atheris.instrument_imports():
    from mowdeck_patterns import (
        Area,
        MowerSpec,
        MowPlan,
        PatternError,
        PatternRegistry,
        check,
        plan,
        preview,
    )
    from mowdeck_patterns.patterns.cells import CellsPattern
    from mowdeck_patterns.patterns.remow import RemowPattern
    from mowdeck_patterns.patterns.rings import (
        BorderFillPattern,
        ContourPattern,
        EdgeOnlyPattern,
        SpiralPattern,
    )
    from mowdeck_patterns.patterns.stripes import (
        AlternatingPattern,
        AutoAlignedPattern,
        CheckerboardPattern,
        DiagonalPattern,
        StripesPattern,
    )

from harness import Input

# The built-in patterns (the entry points of libs/mowdeck-patterns/pyproject.toml): a fuzzer
# binary has no installed package metadata to load entry points from.
REGISTRY = PatternRegistry(entry_point_source=list)
for _pattern in (
    StripesPattern(),
    DiagonalPattern(),
    CheckerboardPattern(),
    AutoAlignedPattern(),
    AlternatingPattern(),
    SpiralPattern(),
    ContourPattern(),
    EdgeOnlyPattern(),
    BorderFillPattern(),
    CellsPattern(),
    RemowPattern(),
):
    REGISTRY.register(_pattern)
KEYS = sorted(REGISTRY.keys())
MOWER = MowerSpec()


def _ring(inp: Input, size: float) -> list[tuple[float, float]]:
    return [
        (round(inp.float_in(0.0, size), 2), round(inp.float_in(0.0, size), 2))
        for _ in range(inp.int_in(3, 8))
    ]


def _settings(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


def test_one_input(data: bytes) -> None:
    inp = Input(data)
    size = inp.float_in(1.0, 30.0)
    boundary = _ring(inp, size)
    holes = [_ring(inp, size) for _ in range(inp.int_in(0, 1))]
    steps: list[tuple[str, dict[str, Any]]] = [
        (inp.pick(KEYS), {}) for _ in range(inp.int_in(1, 2))
    ]
    steps[0] = (steps[0][0], _settings(inp.rest()))
    try:
        area = Area.of(boundary, holes=holes)
        result = plan(area, MOWER, steps, REGISTRY)
    except PatternError:
        return
    for segment in result.segments:
        assert math.isfinite(segment.speed) and segment.speed > 0
    preview(result)
    spot = result.to_spot_plan()
    assert isinstance(check(MowPlan.from_spot_plan(spot), area, MOWER), list | tuple)


def _fraction(value: float) -> bytes:
    return round(value * 0xFFFFFFFF).to_bytes(4, "big")


def seeds() -> list[bytes]:
    """A rectangle and an L shape for every pattern, with its default settings spelled out."""
    shapes = [
        [(0.0, 0.0), (1.0, 0.0), (1.0, 0.5), (0.0, 0.5)],
        [(0.0, 0.0), (1.0, 0.0), (1.0, 0.4), (0.4, 0.4), (0.4, 1.0), (0.0, 1.0)],
    ]
    out = []
    for index, key in enumerate(KEYS):
        schema = REGISTRY.get(key).settings_schema()
        defaults = {
            name: spec["default"]
            for name, spec in schema.get("properties", {}).items()
            if "default" in spec
        }
        for shape in shapes:
            data = _fraction(0.5)  # about 15 m
            data += bytes([len(shape) - 3])
            data += b"".join(_fraction(x) + _fraction(y) for x, y in shape)
            data += bytes([0, 0, index])  # no hole, one step, this pattern
            data += json.dumps(defaults).encode()
            out.append(data)
    return out


def main() -> None:
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
