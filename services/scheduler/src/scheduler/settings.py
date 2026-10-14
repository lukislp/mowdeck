"""Settings of the scheduler service.

Every field is read from ``MOWDECK_<NAME>`` or from the file named by ``MOWDECK_<NAME>_FILE``.
The field docstrings are the configuration reference (README settings table). OpenFGA is
configured with the ``MOWDECK_OPENFGA_*`` variables of ``mowdeck-authz``, token verification
with the ``MOWDECK_AUTH_*`` variables of ``mowdeck-auth``.
"""

from datetime import timedelta
from typing import Literal

from mowdeck_service_kit import ServiceSettings
from pydantic import Field

from scheduler.application.context import SchedulerConfig


class Settings(ServiceSettings):
    """Settings of scheduler: the kit settings plus service defaults."""

    service_name: str = Field(
        default="scheduler", min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9\-]*$"
    )
    """Name of the service in logs, metrics and traces."""

    http_port: int = Field(default=8000, ge=1, le=65535)
    """Port the HTTP server listens on."""

    scheduler_tick_seconds: float = Field(default=30, ge=0.1, le=600)
    """How often every replica looks for due schedules and follows started jobs."""

    scheduler_batch_size: int = Field(default=50, ge=1, le=1000)
    """Schedules one tick fires at most (and started runs it follows)."""

    scheduler_fleet_timeout_seconds: float = Field(default=5, gt=0, le=60)
    """Timeout of the requests to Fleet (``fleet.targets.resolve``, ``fleet.commands.issue``)."""

    scheduler_max_permission_skips: int = Field(default=3, ge=1, le=100)
    """Firings in a row skipped because the owner lost ``can_control`` before the schedule is
    disabled (``scheduler.schedule.disabled``)."""

    scheduler_max_schedules_per_account: int = Field(default=100, ge=1, le=100_000)
    """Schedules an account may have (409 above)."""

    scheduler_run_retention_days: int = Field(default=90, ge=1, le=3650)
    """Run records older than this are deleted (one replica at a time, advisory lock)."""

    scheduler_weather_provider: Literal["open_meteo", "static"] = "open_meteo"
    """Weather provider of the rain pause: ``open_meteo`` (internet access to
    api.open-meteo.com) or ``static`` (tests, development, offline installations)."""

    scheduler_static_weather: Literal["dry", "rain", "unknown"] = "dry"
    """Answer of the static provider at start."""

    scheduler_open_meteo_url: str = "https://api.open-meteo.com"
    """Base URL of the Open-Meteo API (a self-hosted instance or a customer URL)."""

    scheduler_weather_timeout_seconds: float = Field(default=5, gt=0, le=60)
    """Timeout of one forecast request."""

    scheduler_weather_cache_seconds: float = Field(default=1800, ge=0, le=86_400)
    """How long a forecast is reused per location (rounded to about 1 km)."""

    scheduler_dev_endpoints: bool = False
    """Development only: ``PUT /api/v1/scheduler/dev/weather`` switches the static provider."""

    def to_config(self) -> SchedulerConfig:
        """The use case configuration."""
        return SchedulerConfig(
            batch_size=self.scheduler_batch_size,
            max_permission_skips=self.scheduler_max_permission_skips,
            max_schedules_per_account=self.scheduler_max_schedules_per_account,
            run_retention=timedelta(days=self.scheduler_run_retention_days),
        )
