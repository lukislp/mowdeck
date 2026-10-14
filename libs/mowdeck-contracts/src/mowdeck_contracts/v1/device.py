"""Last-value snapshots the Device-Gateway publishes per mower: its map and its capabilities.

The gateway reads both from the adapter after every connect (and the map again after a job
ended, because zones may have changed in the vendor app) and publishes them only when they
differ from what it published before. The stream ``DEVICE`` keeps the newest message per
subject, so a consumer that starts late still finds the current snapshot. Telemetry stores
them and serves them to the web app.
"""

from uuid import UUID

from pydantic import Field

from mowdeck_contracts.v1._base import ContractModel, UtcDateTime
from mowdeck_contracts.v1.capabilities import MowerCapabilities
from mowdeck_contracts.v1.maps import MowerMap


class MowerMapSnapshot(ContractModel):
    """The map of one mower, published on ``device.mower.<id>.map``."""

    mower_id: UUID
    account_id: UUID
    observed_at: UtcDateTime = Field(description="When the gateway read the map (UTC).")
    map: MowerMap


class MowerCapabilitiesSnapshot(ContractModel):
    """The capabilities of one mower, published on ``device.mower.<id>.capabilities``."""

    mower_id: UUID
    account_id: UUID
    observed_at: UtcDateTime = Field(description="When the gateway read the capabilities (UTC).")
    capabilities: MowerCapabilities
