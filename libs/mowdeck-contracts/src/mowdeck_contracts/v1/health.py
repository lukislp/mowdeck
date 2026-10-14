"""Health history of a mower (Telemetry, ADR 0027).

Telemetry derives blade hours, mowing hours, distance, battery cycles, charge sessions and
error counts from the states. When the blade hours since the last blade change cross the
configured threshold it publishes ``telemetry.blade_hours.threshold`` on
``event.telemetry.blade_hours.threshold`` (stream ``EVENTS``) with a
:class:`BladeHoursThreshold` payload, so the Notifications service can remind the owner.
"""

from typing import Final
from uuid import UUID

from pydantic import Field

from mowdeck_contracts.v1._base import ContractModel, UtcDateTime

TELEMETRY_BLADE_HOURS_THRESHOLD: Final = "telemetry.blade_hours.threshold"
"""Event type: the blade hours since the last blade change crossed the threshold
(payload :class:`BladeHoursThreshold`)."""


class BladeHoursThreshold(ContractModel):
    """Payload of ``telemetry.blade_hours.threshold``."""

    mower_id: UUID
    account_id: UUID
    blade_hours_since_change: float = Field(ge=0)
    threshold_hours: float = Field(gt=0)
    blade_changed_at: UtcDateTime | None = Field(
        default=None, description="The last recorded blade change, None when none was recorded."
    )
    crossed_at: UtcDateTime = Field(description="observed_at of the state that crossed it.")
