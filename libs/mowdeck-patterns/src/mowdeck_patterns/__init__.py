"""mowdeck-patterns: mowing patterns as plug-ins and the route validation chain.

A pure computation library (Shapely, no network, no database)::

    from mowdeck_patterns import Area, MowerSpec, plan, preview

    bed = [(9, 5.5), (12, 5.5), (12, 8), (9, 8)]
    area = Area.of([(0, 0), (12, 0), (12, 8), (0, 8)], no_go_zones=[bed])
    result = plan(area, MowerSpec(), [("border_fill", {"laps": 2}), "remow"])  # validated
    geojson = preview(result)
"""

from mowdeck_patterns.base import BasePattern, MowPattern, PatternSettings, build_plan
from mowdeck_patterns.coverage import coverage, covered_ratio, gaps
from mowdeck_patterns.models import (
    Area,
    InvalidArea,
    InvalidSettings,
    MowerSpec,
    MowPlan,
    PatternError,
    PlanningError,
    PlanRejected,
    Point,
    Segment,
)
from mowdeck_patterns.preview import preview
from mowdeck_patterns.registry import (
    ENTRY_POINT_GROUP,
    Chain,
    ChainStep,
    PatternRegistry,
    UnknownPattern,
    chain,
    default_registry,
    plan,
)
from mowdeck_patterns.turns import TURN_STYLES, TurnOptions, TurnResult, shape_turns
from mowdeck_patterns.validation import check, validate

__version__ = "0.1.0"

__all__ = [
    "ENTRY_POINT_GROUP",
    "TURN_STYLES",
    "Area",
    "BasePattern",
    "Chain",
    "ChainStep",
    "InvalidArea",
    "InvalidSettings",
    "MowPattern",
    "MowPlan",
    "MowerSpec",
    "PatternError",
    "PatternRegistry",
    "PatternSettings",
    "PlanRejected",
    "PlanningError",
    "Point",
    "Segment",
    "TurnOptions",
    "TurnResult",
    "UnknownPattern",
    "__version__",
    "build_plan",
    "chain",
    "check",
    "coverage",
    "covered_ratio",
    "default_registry",
    "gaps",
    "plan",
    "preview",
    "shape_turns",
    "validate",
]
