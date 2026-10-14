"""Linked vendor cloud accounts (ADR 0034): device discovery, Fleet to Device-Gateway.

Fleet stores the linked Mammotion accounts of a MowDeck account but never talks to the vendor
cloud itself: the code that does is GPL-3.0 (``mowdeck-control-mammotion``) and runs only in
the Device-Gateway. To check the credentials and list the devices of an account, Fleet sends a
:class:`CloudDiscoverRequest` on :data:`~mowdeck_contracts.v1.subjects.GATEWAY_CLOUD_DISCOVER`
(core NATS request/reply, queue group ``device-gateway``: any instance answers) and gets a
:class:`CloudDiscoverReply` with the devices or an error.

``password`` is a secret: excluded from ``repr``, never logged; the request travels only over
the internal bus, like the credentials in a ``MowerRegistration``.
"""

from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import Field, model_validator

from mowdeck_contracts.v1._base import ContractModel


class CloudProvider(StrEnum):
    """Vendor clouds a linked account can belong to."""

    MAMMOTION = "mammotion"


class DiscoveredDevice(ContractModel):
    """One device of a vendor cloud account as the discovery saw it."""

    device_name: str = Field(
        min_length=1,
        max_length=100,
        description="The device name the vendor app shows, e.g. `Luba-VP12345678`; the key a "
        "mower registration selects the device by.",
    )
    iot_id: str | None = Field(default=None, max_length=100, description="Vendor IoT id.")
    product_key: str | None = Field(default=None, max_length=64)
    model: str | None = Field(
        default=None,
        max_length=100,
        description="Model name as far as known at login (PyMammotion's `extMod`, else the "
        "device type), e.g. `Luba VP` or `LUBA 2 AWD 5000X`.",
    )
    online: bool | None = Field(
        default=None, description="Online flag the cloud reported at login; null when unknown."
    )
    is_mower: bool = Field(
        description="False for RTK base stations, pool cleaners and other non-mowers, which "
        "cannot be registered."
    )


class CloudDiscoverRequest(ContractModel):
    """Request on ``gateway.cloud.discover`` (internal, Fleet to Device-Gateway)."""

    provider: CloudProvider = CloudProvider.MAMMOTION
    account_id: UUID = Field(description="The MowDeck account that links the vendor account.")
    linked_account_id: UUID | None = Field(
        default=None, description="Set for a re-read of a stored linked account; null on create."
    )
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(repr=False, min_length=1, max_length=256)


class CloudDiscoverErrorCode(StrEnum):
    """Why no device list was given."""

    LOGIN_FAILED = "login_failed"
    """The vendor cloud refused the credentials."""
    UNAVAILABLE = "unavailable"
    """The vendor cloud did not answer or failed (try again later)."""
    UNSUPPORTED = "unsupported"
    """The gateway has no adapter for the provider."""


class CloudDiscoverError(ContractModel):
    """The error of a :class:`CloudDiscoverReply`; ``detail`` never echoes the credentials."""

    code: CloudDiscoverErrorCode
    detail: str = Field(max_length=500)


class CloudDiscoverReply(ContractModel):
    """Reply to :class:`CloudDiscoverRequest`: exactly one of ``devices`` or ``error``."""

    devices: list[DiscoveredDevice] | None = Field(default=None, max_length=200)
    error: CloudDiscoverError | None = None
    reused_session: bool = Field(
        default=False,
        description="True when the gateway answered from a cloud session that was already "
        "open for these credentials instead of logging in again.",
    )

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if (self.devices is None) == (self.error is None):
            msg = "exactly one of devices or error must be given"
            raise ValueError(msg)
        return self
