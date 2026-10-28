"""Mowing angle rotation (ADR 0061): a new direction on every run, so no tracks form.

Two ways to rotate, one per schedule:

* **step** (``rotation_step_deg``): the next run of a mower turns the last angle mowed on its
  areas by the step, modulo 180 degrees (stripes have no direction, the Mammotion adapter sends
  ``toward`` modulo 180 too). The last angle lives per mower and area (:class:`AreaAngle`), so
  two schedules for the same lawn and manual starts that opt in continue from each other.
* **list** (``rotation_angles``, ADR 0024): the angles of the list round robin, per schedule.

A job mows several areas with one angle. The run continues from the area that was mowed most
recently (its angle is the lawn's newest direction) and stores the angle it sent for every
area of the job. The first run without any history uses the base angle (the schedule's, else
the mower's mow defaults, else 0). Patterns without a direction (``edge_only``,
``random_angle``) are never rotated and store nothing.
"""

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal
from uuid import UUID

ANGLE_PERIOD: Final = 180.0
"""Angles repeat after half a turn: a stripe at 10 degrees is the stripe at 190 degrees."""

STEP_RANGE: Final = (1.0, 179.0)
"""Smallest and largest rotation step in degrees (0 and 180 would never change the angle)."""

UNDIRECTED_PATTERNS: Final = frozenset({"edge_only", "random_angle"})
"""Patterns whose angle the mower ignores: nothing to rotate."""

AngleSource = Literal["schedule", "manual"]


@dataclass(frozen=True, slots=True)
class AreaAngle:
    """The last angle a job mowed one area of one mower with."""

    mower_id: UUID
    area_id: str
    account_id: UUID
    angle_deg: float
    used_at: datetime
    source: str
    """``schedule`` (a schedule's run, also its manual "run now") or ``manual`` (a start from
    the start sheet with rotation chosen)."""
    schedule_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class MowerRotation:
    """Per mower: the step the start sheet offers for manual starts (None: off)."""

    mower_id: UUID
    account_id: UUID
    manual_step_deg: float | None
    updated_at: datetime


def normalised(angle: float) -> float:
    """``angle`` in ``[0, 180)``, rounded to a tenth of a degree."""
    value = round(angle % ANGLE_PERIOD, 1)
    return 0.0 if value >= ANGLE_PERIOD else value


def valid_step(step: float) -> bool:
    """A step that changes the angle on every run."""
    low, high = STEP_RANGE
    return math.isfinite(step) and low <= step <= high


def directed(pattern_name: object) -> bool:
    """The pattern has a mowing direction (unknown names count as directed)."""
    return pattern_name not in UNDIRECTED_PATTERNS


def last_angle(angles: Iterable[AreaAngle], area_ids: Sequence[str]) -> float | None:
    """The angle of the area of ``area_ids`` mowed most recently; None without any."""
    wanted = set(area_ids)
    newest: AreaAngle | None = None
    for item in angles:
        if item.area_id in wanted and (newest is None or item.used_at > newest.used_at):
            newest = item
    return newest.angle_deg if newest is not None else None


def stepped(last: float | None, step: float, base: float) -> float:
    """The angle after ``last`` turned by ``step``; the base angle when nothing was mowed yet."""
    if last is None:
        return normalised(base)
    return normalised(last + step)


def step_sequence(last: float | None, step: float, base: float, count: int) -> list[float]:
    """The next ``count`` angles of step rotation, each run continuing from the one before."""
    result: list[float] = []
    current = last
    for _ in range(count):
        current = stepped(current, step, base)
        result.append(current)
    return result


def list_sequence(rotation: Sequence[float], index: int, count: int) -> list[float]:
    """The next ``count`` angles of a rotation list from ``index`` on."""
    if not rotation:
        return []
    return [float(rotation[(index + k) % len(rotation)]) % ANGLE_PERIOD for k in range(count)]
