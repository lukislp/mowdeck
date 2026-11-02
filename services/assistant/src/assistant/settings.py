"""Settings of the assistant service.

Every field is read from ``MOWDECK_<NAME>`` or from the file named by ``MOWDECK_<NAME>_FILE``.
The field docstrings are the configuration reference (README settings table).
"""

from typing import Literal

from mowdeck_service_kit import ServiceSettings
from pydantic import Field


class Settings(ServiceSettings):
    """Settings of the assistant: the kit settings plus service defaults."""

    service_name: str = Field(
        default="assistant", min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9\-]*$"
    )
    """Name of the service in logs, metrics and traces."""

    http_port: int = Field(default=8000, ge=1, le=65535)
    """Port the HTTP server listens on."""

    nats_enabled: bool = False
    """The assistant needs no bus: it only calls the public HTTP API (ADR 0075)."""

    assistant_interpreter: Literal["provider", "phrases"] = "provider"
    """``provider``: the user's AI provider through Vision (Settings, AI provider);
    ``phrases``: the built-in phrase parser without any AI. Demo mode always uses ``phrases``."""

    assistant_identity_url: str = Field(default="http://identity:8000", min_length=1)
    """Base URL of Identity (permission checks)."""

    assistant_fleet_url: str = Field(default="http://fleet:8000", min_length=1)
    """Base URL of Fleet (the mower and its area names)."""

    assistant_telemetry_url: str = Field(default="http://telemetry:8000", min_length=1)
    """Base URL of Telemetry (map, capabilities, state)."""

    assistant_vision_url: str = Field(default="http://vision:8000", min_length=1)
    """Base URL of Vision (the AI provider settings and structured answers)."""

    assistant_upstream_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    """Timeout of one read from Identity, Fleet or Telemetry."""

    assistant_ai_timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    """Timeout of one structured answer from the AI provider."""

    assistant_requests_per_minute: int = Field(default=20, ge=1, le=1000)
    """Proposals per user and minute (per replica)."""

    @property
    def interpreter(self) -> Literal["provider", "phrases"]:
        """The interpreter in effect: always the phrase parser in demo mode (ADR 0068: no
        outbound traffic to third parties)."""
        return "phrases" if self.demo_mode else self.assistant_interpreter
