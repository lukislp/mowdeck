"""Translate a neutral mowing request into a native mower job.

This is the seam between the Planner (neutral plans, ADR 0030) and the mower: when the mower can
run a pattern itself on its own map areas, the request becomes a :class:`NativeJob`; otherwise
:class:`~mowdeck_control.errors.UnsupportedPattern` is raised and the caller must either refuse
or, only when ``capabilities.custom_paths`` is True, drive its own route (``StartMow.spot_plan``).

The pattern names follow the Planner plug-ins. The native mapping mirrors what PyMammotion
offers (spike 0001, section 3.2): stripes at any angle, checkerboard as a second pass at a
crossing angle, border laps 0 to 4 and the order border first or grid first. The job options
(obstacle detection, laps around no-go zones, path order, start progress; ADR 0030 amendment of
2026-10-24) are checked against ``capabilities.start_options`` and handed on unchanged; one left
None stays the mower's own default.
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from mowdeck_contracts.v1 import (
    MowerCapabilities,
    MowPattern,
    ObstacleDetection,
    PathOrder,
    StartMow,
)
from pydantic import ValidationError

from mowdeck_control.errors import UnsupportedPattern

MAX_BORDER_LAPS = 4
DEFAULT_SPACING_M = 0.25


MowOrder = PathOrder
"""Whether border laps run before or after the fill (the contracts' ``PathOrder``)."""

JOB_OPTION_KEYS: tuple[str, ...] = (
    "obstacle_detection",
    "obstacle_laps",
    "order",
    "start_progress_pct",
)
"""``StartMow.pattern`` keys of the job options (``MowPattern``, ``StartOptions``)."""


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
    order: MowOrder | None = None
    """None: the mower's own order."""
    obstacle_detection: ObstacleDetection | None = None
    obstacle_laps: int | None = None
    start_progress_pct: int | None = None

    @classmethod
    def from_start_mow(cls, command: StartMow) -> "MowRequest":
        """Build a request from a ``StartMow`` with ``area_ids``.

        ``command.pattern`` may contain ``name``, ``angle_deg``, ``spacing_m``,
        ``border_laps`` and the job options of :data:`JOB_OPTION_KEYS` (validated as in
        ``MowPattern``). Raises ``ValueError`` for a plan reference (needs the Planner) or
        malformed settings.
        """
        if not command.area_ids:
            msg = "plan references need the Planner; only area_ids can be translated"
            raise ValueError(msg)
        settings: Mapping[str, Any] = command.pattern or {}
        try:
            options = MowPattern.model_validate(
                {key: settings[key] for key in JOB_OPTION_KEYS if settings.get(key) is not None}
            )
            request = cls(
                area_ids=tuple(command.area_ids),
                pattern=str(settings.get("name", Pattern.STRIPES)),
                angle_deg=float(settings.get("angle_deg", 0.0)),
                spacing_m=float(settings.get("spacing_m", DEFAULT_SPACING_M)),
                border_laps=int(settings.get("border_laps", 0)),
                order=options.order,
                obstacle_detection=options.obstacle_detection,
                obstacle_laps=options.obstacle_laps,
                start_progress_pct=options.start_progress_pct,
            )
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                for error in exc.errors()
            )
            msg = f"invalid pattern settings: {problems}"
            raise ValueError(msg) from None
        except (TypeError, ValueError) as exc:
            msg = f"invalid pattern settings: {exc}"
            raise ValueError(msg) from exc
        return request


@dataclass(frozen=True, slots=True)
class NativeJob:
    """A job the mower runs by itself on its own areas.

    ``passes`` lists the stripe angles in degrees: one for stripes and diagonal, two for
    checkerboard, none for edge only. The job options are None where the start left them out
    (the mower keeps its own default); ``start_progress_pct`` 0 is the same as None.
    """

    area_ids: tuple[str, ...]
    pattern: str
    passes: tuple[float, ...]
    spacing_m: float
    border_laps: int
    order: MowOrder | None
    obstacle_detection: ObstacleDetection | None = None
    obstacle_laps: int | None = None
    start_progress_pct: int | None = None


def without_undeclared_options(
    pattern: Mapping[str, Any], capabilities: MowerCapabilities
) -> dict[str, Any]:
    """``pattern`` without the job options (:data:`JOB_OPTION_KEYS`) whose value the mower does
    not declare in ``capabilities.start_options``, so it keeps its own setting for them.

    For settings MowDeck derives itself rather than the user chose, e.g. the Planner's
    ``border_fill``, which asks for ``order: border_first`` on every mower that offers it.
    """
    result = dict(pattern)
    for key in JOB_OPTION_KEYS:
        value = result.get(key)
        if value is None:
            continue
        try:
            option = MowPattern.model_validate({key: value})
        except ValidationError:
            continue  # malformed: left for the start to refuse
        if capabilities.start_options.problem(option) is not None:
            del result[key]
    return result


def plan_to_native_job(plan: MowRequest, capabilities: MowerCapabilities) -> NativeJob:
    """Map ``plan`` to a :class:`NativeJob`.

    Raises :class:`UnsupportedPattern` when the mower cannot run the pattern natively and
    ``ValueError`` for out-of-range settings, including a job option the mower does not declare
    in ``capabilities.start_options``.
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
    problem = capabilities.start_options.problem(
        MowPattern(
            obstacle_detection=plan.obstacle_detection,
            obstacle_laps=plan.obstacle_laps,
            order=plan.order,
            start_progress_pct=plan.start_progress_pct,
        )
    )
    if problem is not None:
        raise ValueError(problem)
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
        obstacle_detection=plan.obstacle_detection,
        obstacle_laps=plan.obstacle_laps,
        start_progress_pct=plan.start_progress_pct or None,
    )
