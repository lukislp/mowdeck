"""Configuration of the use cases (built from ``Settings`` in ``main``)."""

from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True, slots=True)
class PlannerConfig:
    """Limits and the default mower data of the Planner."""

    cutting_width_m: float = 0.4
    overlap_m: float = 0.05
    turning_radius_m: float = 0.0
    mowing_speed_mps: float = 0.4
    transfer_speed_mps: float = 0.6
    safety_margin_m: float = 0.15
    max_vertices: int = 2000
    max_area_m2: float = 20_000.0
    max_plan_bytes: int = 262_144
    max_parallel: int = 2
    max_templates: int = 100
    track_limit: int = 20_000
    track_timeout_seconds: float = 5.0
    coverage_max_range: timedelta = timedelta(days=2)
    max_gaps: int = 200
    spot_tolerance_m: float = 0.05
    """How far a drawn spot may reach beyond the mower's mapped areas (drawing slack)."""
    turn_radius_m: float = 0.3
    """Smallest arc radius of shaped lane-end turns (ADR 0043): the bulb and three-point turns
    use it; at least the mower's turning radius."""
