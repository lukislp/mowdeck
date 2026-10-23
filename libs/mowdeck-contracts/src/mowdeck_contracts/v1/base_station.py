"""RTK base stations (ADR 0047): the reference station a mower takes its RTK corrections from.

A base station is its own device at the vendor (own cloud id) but has no connection of its own in
MowDeck: Fleet keeps it as a ``base_station`` record linked to the mowers that use it, and the
Device-Gateway learns about the link through :attr:`MowerRegistration.base_station
<mowdeck_contracts.v1.MowerRegistration.base_station>` (:class:`BaseStationRef`). The mower's
connection reports what the mower knows about its base in
:attr:`MowerState.base_station <mowdeck_contracts.v1.MowerState.base_station>`
(:class:`BaseStationState`): the correction link and its health come from the mower's own reports,
the details (satellites, solution, Wi-Fi) from an occasional request the mower answers, the
firmware from the vendor's firmware service asked with the base station's id.

Every field inside the state is nullable: an adapter fills what its mower reports and leaves the
rest null, never a made-up zero.
"""

from enum import StrEnum
from typing import Final, Literal
from uuid import UUID

from pydantic import Field

from mowdeck_contracts.v1._base import ContractModel, UtcDateTime
from mowdeck_contracts.v1.firmware import FirmwareInfo
from mowdeck_contracts.v1.mower_status import GnssSolution

BASE_STATION_OPTION: Final = "base_station"
"""Connection option the Device-Gateway sets from :attr:`MowerRegistration.base_station
<mowdeck_contracts.v1.MowerRegistration.base_station>` (the :class:`BaseStationRef` as JSON), so
the adapter knows which base station the mower uses. Adapters remove it before they parse their
own options."""


class BaseStationRef(ContractModel):
    """The base station a registered mower uses (``MowerRegistration.base_station``)."""

    base_station_id: UUID = Field(description="Fleet's id of the base station.")
    name: str = Field(min_length=1, max_length=100, description="The name the owner gave it.")
    source: Literal["cloud", "simulated"] = Field(
        default="cloud",
        description="`cloud`: a device of a linked vendor account (`device_name`, `iot_id` set); "
        "`simulated`: the simulator's base station.",
    )
    device_name: str | None = Field(
        default=None, max_length=100, description="The vendor's device name, e.g. `RTK12345678`."
    )
    iot_id: str | None = Field(
        default=None,
        max_length=100,
        description="The vendor's device id; the key of the firmware service.",
    )


class RtkCorrectionLink(StrEnum):
    """How the RTK corrections reach the mower."""

    LORA = "lora"
    """Directly from the own base station over LoRa radio."""
    INTERNET = "internet"
    """From the own base station through the vendor cloud (the base is online by Wi-Fi)."""
    NETWORK = "network"
    """From a network RTK service (NRTK); the own base station is not used."""
    UNKNOWN = "unknown"


class BaseStationPosition(ContractModel):
    """Where the base station stands (WGS84 degrees), as the mower knows it."""

    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class BaseStationState(ContractModel):
    """The RTK base station of a mower as its connection sees it (``MowerState.base_station``).

    Null on the mower state when the mower is linked to no base station in Fleet.
    """

    base_station_id: UUID | None = Field(
        default=None, description="Fleet's id of the base station (from the registration)."
    )
    correction_link: RtkCorrectionLink = Field(
        default=RtkCorrectionLink.UNKNOWN,
        description="How the corrections reach the mower now; `network` means the mower does "
        "not use its base station at the moment.",
    )
    link_up: bool | None = Field(
        default=None,
        description="Whether the mower receives the base station: over LoRa the radio link "
        "the mower reports, over the internet whether corrections arrive; null when the mower "
        "does not say (or uses network RTK).",
    )
    lora_channel: int | None = Field(
        default=None, ge=0, le=255, description="The LoRa channel of the pairing, if reported."
    )
    satellites: int | None = Field(
        default=None, ge=0, le=255, description="Satellites the base station's receiver uses."
    )
    rtk_status: GnssSolution = Field(
        default=GnssSolution.UNKNOWN, description="The base station's own position solution."
    )
    position: BaseStationPosition | None = Field(
        default=None, description="Where the base station stands; null while not reported."
    )
    wifi_dbm: int | None = Field(
        default=None,
        ge=-150,
        le=-1,
        description="The base station's Wi-Fi signal in dBm; null when not reported.",
    )
    moved: bool | None = Field(
        default=None,
        description="The mower reports that the base station was moved since the map was "
        "recorded (positions may be shifted); null when not reported.",
    )
    firmware: str | None = Field(
        default=None, max_length=64, description="The base station's firmware, as reported."
    )
    firmware_info: FirmwareInfo | None = Field(
        default=None,
        description="Installed and offered firmware of the base station from the vendor's "
        "firmware service, and the update state; null before a check.",
    )
    details_at: UtcDateTime | None = Field(
        default=None,
        description="When the details (`satellites`, `rtk_status`, `wifi_dbm`) were last "
        "reported; null when never.",
    )
