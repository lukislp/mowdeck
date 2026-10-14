"""Translate a neutral mowing request into a native mower job.

This is the seam between the Planner (neutral plans, ADR 0030) and the mower: when the mower can
run a pattern itself on its own map areas, the request becomes a :class:`NativeJob`; otherwise
:class:`~mowdeck_control.errors.UnsupportedPattern` is raised and the caller must either refuse
or, only when ``capabilities.custom_paths`` is True, drive its own route (``StartMow.spot_plan``).

The pattern names follow the Planner plug-ins. The native mapping mirrors what PyMammotion
offers (spike 0001, section 3.2): stripes at any angle, checkerboard as a second pass at a
crossing angle, border laps 0 to 4 and the order border first or grid first.
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from mowdeck_contracts.v1 import MowerCapabilities, StartMow

from mowdeck_control.errors import UnsupportedPattern

MAX_BORDER_LAPS = 4
DEFAULT_SPACING_M = 0.25


class MowOrder(StrEnum):
    """Whether border laps run before or after the fill."""

    BORDER_FIRST = "border_first"
    GRID_FIRST = "grid_first"


class Pattern(StrEnum):
    """Neutral pattern names that may map onto native jobs."""

    STRIPES = "stripes"
    DIAGONAL = "diagonal"
    CHECKERBOARD = "checkerboard"
    EDGE_ONLY = "edge_only"


@dataclass(frozen=True, slots=True)
class MowRequest:
    """A neutral "mow these areas with this pattern" request."""

    area_ids: tuple[str, ...]
    pattern: str = Pattern.STRIPES
    angle_deg: float = 0.0
    spacing_m: float = DEFAULT_SPACING_M
    border_laps: int = 0
    order: MowOrder = MowOrder.BORDER_FIRST

    @classmethod
    def from_start_mow(cls, command: StartMow) -> "MowRequest":
        """Build a request from a ``StartMow`` with ``area_ids``.

        ``command.pattern`` may contain ``name``, ``angle_deg``, ``spacing_m``,
        ``border_laps`` and ``order``. Raises ``ValueError`` for a plan reference (needs the
        Planner) or malformed settings.
        """
        if not command.area_ids:
            msg = "plan references need the Planner; only area_ids can be translated"
            raise ValueError(msg)
        settings: Mapping[str, Any] = command.pattern or {}
        try:
            request = cls(
                area_ids=tuple(command.area_ids),
                pattern=str(settings.get("name", Pattern.STRIPES)),
                angle_deg=float(settings.get("angle_deg", 0.0)),
                spacing_m=float(settings.get("spacing_m", DEFAULT_SPACING_M)),
                border_laps=int(settings.get("border_laps", 0)),
                order=MowOrder(settings.get("order", MowOrder.BORDER_FIRST)),
            )
        except (TypeError, ValueError) as exc:
            msg = f"invalid pattern settings: {exc}"
            raise ValueError(msg) from exc
        return request


@dataclass(frozen=True, slots=True)
class NativeJob:
    """A job the mower runs by itself on its own areas.

    ``passes`` lists the stripe angles in degrees: one for stripes and diagonal, two for
    checkerboard, none for edge only.
    """

    area_ids: tuple[str, ...]
    pattern: str
    passes: tuple[float, ...]
    spacing_m: float
    border_laps: int
    order: MowOrder


def plan_to_native_job(plan: MowRequest, capabilities: MowerCapabilities) -> NativeJob:
    """Map ``plan`` to a :class:`NativeJob`.

    Raises :class:`UnsupportedPattern` when the mower cannot run the pattern natively and
    ``ValueError`` for out-of-range settings.
    """
    if not capabilities.supports_pattern(plan.pattern):
        msg = f"pattern '{plan.pattern}' is not supported natively by this mower"
        raise UnsupportedPattern(msg)
    if not plan.area_ids:
        msg = "at least one area id is required"
        raise ValueError(msg)
    if not (math.isfinite(plan.spacing_m) and 0.05 <= plan.spacing_m <= 2.0):
        msg = "spacing_m must be between 0.05 and 2.0"
        raise ValueError(msg)
    if not 0 <= plan.border_laps <= MAX_BORDER_LAPS:
        msg = f"border_laps must be between 0 and {MAX_BORDER_LAPS}"
        raise ValueError(msg)
    if not math.isfinite(plan.angle_deg):
        msg = "angle_deg must be finite"
        raise ValueError(msg)
    angle = plan.angle_deg % 180.0
    passes: tuple[float, ...]
    border_laps = plan.border_laps
    match plan.pattern:
        case Pattern.DIAGONAL:
            passes = ((angle + 45.0) % 180.0,)
        case Pattern.CHECKERBOARD:
            passes = (angle, (angle + 90.0) % 180.0)
        case Pattern.EDGE_ONLY:
            passes = ()
            border_laps = max(1, border_laps)
        case _:
            passes = (angle,)
    return NativeJob(
        area_ids=plan.area_ids,
        pattern=plan.pattern,
        passes=passes,
        spacing_m=plan.spacing_m,
        border_laps=border_laps,
        order=plan.order,
    )
