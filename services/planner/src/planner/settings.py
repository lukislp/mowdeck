"""Settings of the planner service.

Every field is read from ``MOWDECK_<NAME>`` or from the file named by ``MOWDECK_<NAME>_FILE``.
The field docstrings are the configuration reference (README settings table). OpenFGA is
configured with the ``MOWDECK_OPENFGA_*`` variables of ``mowdeck-authz``, token verification
with the ``MOWDECK_AUTH_*`` variables of ``mowdeck-auth``.
"""

from datetime import timedelta

from mowdeck_service_kit import ServiceSettings
from pydantic import Field

from planner.application.context import PlannerConfig


class Settings(ServiceSettings):
    """Settings of planner: the kit settings plus service defaults."""

    service_name: str = Field(
        default="planner", min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9\-]*$"
    )
    """Name of the service in logs, metrics and traces."""

    http_port: int = Field(default=8000, ge=1, le=65535)
    """Port the HTTP server listens on."""

    planner_cutting_width_m: float = Field(default=0.4, ge=0.05, le=2)
    """Cutting width of the mowers in metres (LUBA 2: 0.4 m), unless a request overrides it."""

    planner_overlap_m: float = Field(default=0.05, ge=0, le=1)
    """Overlap of neighbouring runs; the spacing is the cutting width minus this."""

    planner_turning_radius_m: float = Field(default=0.0, ge=0, le=10)
    """Turning radius checked by the validation chain (0: the mower turns on the spot)."""

    planner_turn_radius_m: float = Field(default=0.3, ge=0.05, le=5)
    """Smallest arc radius of the lane-end turns of a spot plan with a turn style (ADR 0043)."""

    planner_mowing_speed_mps: float = Field(default=0.4, gt=0, le=2)
    """Mowing speed of the plans (capped by the mower's ``max_speed_mps``)."""

    planner_transfer_speed_mps: float = Field(default=0.6, gt=0, le=2)
    """Speed of transfers with the blades off (capped by the mower's ``max_speed_mps``)."""

    planner_safety_margin_m: float = Field(default=0.15, ge=0, le=2)
    """Distance the mower's centre keeps from every edge, hole and zone."""

    planner_max_vertices: int = Field(default=2000, ge=10, le=5000)
    """Most vertices a drawn spot (outline and holes) may have (422 above)."""

    planner_max_area_m2: float = Field(default=20_000, ge=1, le=50_000)
    """Largest area one plan may cover (422 above)."""

    planner_max_plan_bytes: int = Field(default=262_144, ge=1024, le=900_000)
    """Largest plan as JSON (Fleet's ``spot_plan_max_bytes`` must not be smaller)."""

    planner_max_parallel: int = Field(default=2, ge=1, le=32)
    """Plans computed at the same time per replica (worker threads); more requests wait."""

    planner_max_templates: int = Field(default=100, ge=1, le=10_000)
    """Templates an account may keep (409 above)."""

    planner_track_timeout_seconds: float = Field(default=5, gt=0, le=60)
    """Timeout of the position track request to Telemetry."""

    planner_track_limit: int = Field(default=20_000, ge=100, le=50_000)
    """Most track points one coverage query reads."""

    planner_coverage_max_hours: int = Field(default=48, ge=1, le=24 * 31)
    """Longest time range of a coverage query."""

    def to_config(self) -> PlannerConfig:
        """The use case configuration."""
        return PlannerConfig(
            cutting_width_m=self.planner_cutting_width_m,
            overlap_m=self.planner_overlap_m,
            turning_radius_m=self.planner_turning_radius_m,
            turn_radius_m=self.planner_turn_radius_m,
            mowing_speed_mps=self.planner_mowing_speed_mps,
            transfer_speed_mps=self.planner_transfer_speed_mps,
            safety_margin_m=self.planner_safety_margin_m,
            max_vertices=self.planner_max_vertices,
            max_area_m2=self.planner_max_area_m2,
            max_plan_bytes=self.planner_max_plan_bytes,
            max_parallel=self.planner_max_parallel,
            max_templates=self.planner_max_templates,
            track_limit=self.planner_track_limit,
            track_timeout_seconds=self.planner_track_timeout_seconds,
            coverage_max_range=timedelta(hours=self.planner_coverage_max_hours),
        )
