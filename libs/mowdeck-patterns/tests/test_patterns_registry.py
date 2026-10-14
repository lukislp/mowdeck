"""Entry point discovery, the registry and chains."""

import json
from collections.abc import Iterable, Mapping
from importlib.metadata import EntryPoint
from typing import Any, ClassVar

import pytest
from mowdeck_patterns import (
    Area,
    MowerSpec,
    MowPlan,
    PatternError,
    PatternRegistry,
    PlanningError,
    PlanRejected,
    Segment,
    UnknownPattern,
    chain,
    default_registry,
    plan,
)
from mowdeck_patterns.base import BasePattern, MowPattern, PatternSettings
from mowdeck_patterns.patterns import BUILTIN_PATTERNS
from patterns_support import MOWER, front_area, holed
from pydantic import Field

BUILTIN_KEYS = {
    "stripes",
    "diagonal",
    "checkerboard",
    "spiral",
    "border_fill",
    "contour",
    "auto_aligned",
    "cells",
    "alternating",
    "edge_only",
    "remow",
}


class OutsidePattern:
    """A third-party pattern without the base class that drives out of the area."""

    key = "outside"
    name = "Outside"

    def settings_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    def compute(self, area: Area, mower: MowerSpec, settings: Mapping[str, Any]) -> MowPlan:
        return MowPlan(
            segments=(Segment(((1.0, 1.0), (50.0, 1.0)), True, 0.3),),
            estimated_duration_s=1.0,
            covered_area_m2=1.0,
            pattern_chain=(self.key,),
        )


def _source(*entries: EntryPoint) -> Any:
    def source() -> Iterable[EntryPoint]:
        return entries

    return source


def test_entry_points_provide_every_builtin_pattern() -> None:
    keys = set(default_registry().keys())
    assert keys >= BUILTIN_KEYS
    assert set(BUILTIN_PATTERNS) == BUILTIN_KEYS
    for pattern in default_registry().all():
        assert isinstance(pattern, MowPattern)


def test_registry_loads_entry_points_and_skips_broken_ones(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = PatternRegistry(
        _source(
            EntryPoint("stripes", "mowdeck_patterns.patterns.stripes:StripesPattern", "g"),
            EntryPoint("missing", "mowdeck_patterns.nothing:Nope", "g"),
            EntryPoint("wrong", "mowdeck_patterns.patterns.stripes:DiagonalPattern", "g"),
            EntryPoint("plain", "json:dumps", "g"),
        )
    )
    assert registry.load_entry_points() == ["stripes"]
    assert registry.keys() == ["stripes"]
    assert "cannot load pattern entry point" in caplog.text
    with pytest.raises(UnknownPattern):
        registry.get("diagonal")


def test_registry_register_and_replace() -> None:
    registry = PatternRegistry(_source())
    outside = OutsidePattern()
    registry.register(outside)
    registry.register(outside)  # the same object again is fine
    with pytest.raises(ValueError, match="already registered"):
        registry.register(OutsidePattern())
    registry.register(OutsidePattern(), replace=True)
    with pytest.raises(TypeError):
        registry.register(json)  # type: ignore[arg-type]
    assert [p.key for p in registry.all()] == ["outside"]


def test_settings_schemas_are_json_schemas() -> None:
    for key, cls in BUILTIN_PATTERNS.items():
        schema = cls().settings_schema()
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert schema["title"]
        assert "speed_mps" in schema["properties"]
        json.dumps(schema)  # serialisable
        cls().parse({}) if key != "remow" else None  # defaults are valid


def test_chain_joins_patterns_with_a_transfer() -> None:
    area = front_area()
    combined = chain([("edge_only", {"laps": 2}), ("diagonal", {})]).compute(area, MOWER)
    assert combined.pattern_chain == ("edge_only", "diagonal")
    edge = default_registry().get("edge_only").compute(area, MOWER, {"laps": 2})
    assert combined.segments[: len(edge.segments)] == edge.segments
    validated = plan(area, MOWER, [("edge_only", {"laps": 2}), "diagonal"])
    assert validated.estimated_duration_s > edge.estimated_duration_s


def test_chain_of_a_bad_plugin_is_rejected_by_the_chain() -> None:
    registry = PatternRegistry(_source())
    registry.register(OutsidePattern())
    with pytest.raises(PlanRejected):
        plan(front_area(), MOWER, ["outside"], registry)


def test_chain_limits() -> None:
    with pytest.raises(PatternError):
        chain([])
    with pytest.raises(PatternError):
        chain(["stripes"] * 9)
    with pytest.raises(UnknownPattern):
        chain(["waves"])


def test_chain_transfers_avoid_holes() -> None:
    area = holed()
    result = plan(area, MOWER, ["edge_only", ("cells", {}), ("spiral", {})])
    assert result.pattern_chain == ("edge_only", "cells", "spiral")
    assert any(s.kind == "transfer" for s in result.segments)


def test_split_areas_cannot_be_planned_in_one_go() -> None:
    split = Area.of(
        [(0, 0), (10, 0), (10, 4), (0, 4)], no_go_zones=[[(4.8, -1), (5.2, -1), (5.2, 5), (4.8, 5)]]
    )
    with pytest.raises(PlanningError, match="falls apart"):
        plan(split, MOWER, ["stripes"])


class FanSettings(PatternSettings):
    angles_deg: list[float] = Field(default_factory=lambda: [0.0, 60.0, 120.0])


class FanPattern(BasePattern[FanSettings]):
    """The README's example of a custom pattern."""

    key: ClassVar[str] = "fan"
    name: ClassVar[str] = "Fan"
    settings_model: ClassVar[type[PatternSettings]] = FanSettings

    def runs(self, workspace: Any, settings: FanSettings, start: Any) -> list[Any]:
        from mowdeck_patterns.geometry import stripe_runs

        runs: list[Any] = []
        for angle in settings.angles_deg:
            runs.extend(stripe_runs(workspace.work, angle, workspace.mower.spacing_m, start))
        return runs


def test_a_custom_pattern_plugs_in() -> None:
    registry = PatternRegistry(_source())
    registry.register(FanPattern())
    result = plan(front_area(), MOWER, [("fan", {"angles_deg": [0, 90]})], registry)
    assert result.pattern_chain == ("fan",)
    assert result.coverage_ratio > 0.95
    assert BasePattern.native_job(FanPattern(), {}, MOWER) is None
    with pytest.raises(NotImplementedError):
        BasePattern.runs(FanPattern(), None, None, None)  # type: ignore[arg-type, misc]
