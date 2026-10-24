"""Start settings per schedule (ADR 0024 amendment 2026-10-24) and the capabilities per mower.

* ``schedules.blade_height_mm`` and ``schedules.speed_mps``: setpoints sent before each start,
  null (the mower keeps its own) for every existing schedule.
* ``schedules.pattern`` holds only ``SchedulePattern`` keys from now on. Existing patterns are
  rewritten once: the legacy ``angle`` (written by the web form so far) becomes ``angle_deg``
  normalised to 0 up to 180 degrees, as the runner already sent it; keys and values outside the
  contract are dropped, a pattern left empty becomes null. Every existing schedule therefore
  starts exactly as before, with all new options unset.
* ``mower_capabilities``: the newest ``MowerCapabilitiesSnapshot`` per mower, to check a
  schedule's settings against its mowers and to fit each run to the mower.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-24
"""

import json
import math
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)

_NAMES = frozenset({"stripes", "diagonal", "checkerboard", "edge_only", "random_angle"})
_DETECTION = frozenset({"off", "slow_touch", "less_touch", "standard", "sensitive"})
_ORDERS = frozenset({"border_first", "grid_first"})


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def _whole(value: Any, low: int, high: int) -> int | None:
    number = _number(value)
    if number is None or not number.is_integer() or not low <= number <= high:
        return None
    return int(number)


def normalised_pattern(raw: Any) -> dict[str, Any] | None:
    """The ``SchedulePattern`` keys of a stored pattern of revision 0002 (a free dict)."""
    if not isinstance(raw, dict):
        return None
    result: dict[str, Any] = {}
    if raw.get("name") in _NAMES:
        result["name"] = raw["name"]
    angle = _number(raw["angle_deg"] if "angle_deg" in raw else raw.get("angle"))
    if angle is not None:
        result["angle_deg"] = angle % 180.0
    laps = _whole(raw.get("border_laps"), 0, 4)
    if laps is not None:
        result["border_laps"] = laps
    spacing = _number(raw.get("spacing_m"))
    if spacing is not None and 0.05 <= spacing <= 2.0:
        result["spacing_m"] = spacing
    if raw.get("obstacle_detection") in _DETECTION:
        result["obstacle_detection"] = raw["obstacle_detection"]
    obstacle_laps = _whole(raw.get("obstacle_laps"), 0, 4)
    if obstacle_laps is not None:
        result["obstacle_laps"] = obstacle_laps
    if raw.get("order") in _ORDERS:
        result["order"] = raw["order"]
    return result or None


def upgrade() -> None:
    """Add the setpoint columns and the capabilities table; rewrite stored patterns."""
    op.add_column("schedules", sa.Column("blade_height_mm", sa.Integer(), nullable=True))
    op.add_column("schedules", sa.Column("speed_mps", sa.Float(), nullable=True))
    op.create_table(
        "mower_capabilities",
        sa.Column("mower_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("observed_at", TS, nullable=False),
        sa.Column("capabilities", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("mower_id", name="pk_mower_capabilities"),
    )
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, pattern FROM schedules WHERE pattern IS NOT NULL"))
    for row in rows.fetchall():
        pattern = row.pattern if not isinstance(row.pattern, str) else json.loads(row.pattern)
        cleaned = normalised_pattern(pattern)
        if cleaned == pattern:
            continue
        conn.execute(
            sa.text("UPDATE schedules SET pattern = CAST(:pattern AS jsonb) WHERE id = :id"),
            {"pattern": None if cleaned is None else json.dumps(cleaned), "id": row.id},
        )


def downgrade() -> None:
    """Drop what ``upgrade`` added; rewritten patterns stay (revision 0002 reads them)."""
    op.drop_table("mower_capabilities")
    op.drop_column("schedules", "speed_mps")
    op.drop_column("schedules", "blade_height_mm")
