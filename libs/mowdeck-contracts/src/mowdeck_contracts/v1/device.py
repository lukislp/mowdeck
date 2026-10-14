"""Last-value snapshots the Device-Gateway publishes per mower: its map, its capabilities and
the mow path of the running job.

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
from mowdeck_contracts.v1.mow_path import MowPath


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


class MowerPathSnapshot(ContractModel):
    """The mow path of the running job, published on ``device.mower.<id>.path``.

    Published when the path or the progress along it changed; ``path`` is None once the job
    ended (the last value per subject then tells late consumers that there is no path).
    """

    mower_id: UUID
    account_id: UUID
    observed_at: UtcDateTime = Field(description="When the gateway read the path (UTC).")
    path: MowPath | None = None
