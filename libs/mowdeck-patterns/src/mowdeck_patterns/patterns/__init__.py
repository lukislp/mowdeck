"""The built-in patterns (registered as entry points in this package's ``pyproject.toml``)."""

from typing import Any

from mowdeck_patterns.base import BasePattern
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

BUILTIN_PATTERNS: dict[str, type[BasePattern[Any]]] = {
    cls.key: cls
    for cls in (
        StripesPattern,
        DiagonalPattern,
        CheckerboardPattern,
        SpiralPattern,
        BorderFillPattern,
        ContourPattern,
        AutoAlignedPattern,
        CellsPattern,
        AlternatingPattern,
        EdgeOnlyPattern,
        RemowPattern,
    )
}
"""Key to class of every built-in pattern, in the order of the project plan's table."""

__all__ = [
    "BUILTIN_PATTERNS",
    "AlternatingPattern",
    "AutoAlignedPattern",
    "BorderFillPattern",
    "CellsPattern",
    "CheckerboardPattern",
    "ContourPattern",
    "DiagonalPattern",
    "EdgeOnlyPattern",
    "RemowPattern",
    "SpiralPattern",
    "StripesPattern",
]
