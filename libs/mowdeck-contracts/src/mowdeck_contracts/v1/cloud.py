"""Linked vendor cloud accounts (ADR 0034): device discovery, Fleet to Device-Gateway.

Fleet stores the linked vendor accounts of a MowDeck account but never talks to a vendor cloud
itself: that code lives in the adapter packages (some GPL-3.0) and runs only in the
Device-Gateway. To check the credentials and list the devices of an account, Fleet sends a
:class:`CloudDiscoverRequest` on :data:`~mowdeck_contracts.v1.subjects.GATEWAY_CLOUD_DISCOVER`
(core NATS request/reply, queue group ``device-gateway``: any instance answers) and gets a
:class:`CloudDiscoverReply` with the devices or an error. Which providers exist and which
credentials they take is the provider's :class:`~mowdeck_contracts.v1.providers.\
ProviderDescriptor` (ADR 0066).

``credentials`` (and the deprecated ``password``) are secrets: excluded from ``repr``, never
logged; the request travels only over the internal bus, like the credentials in a
``MowerRegistration``.
"""

from enum import StrEnum
from typing import Any, Self
from uuid import UUID

from pydantic import Field, model_validator

from mowdeck_contracts.v1._base import ContractModel
from mowdeck_contracts.v1.providers import MAX_CREDENTIAL_LENGTH, PROVIDER_ID_PATTERN


class DeviceType(StrEnum):
    """What a discovered device is (ADR 0047)."""

    MOWER = "mower"
    """A mower: registered as a mower."""
    RTK_BASE = "rtk_base"
    """An RTK base station: registered as a base station and linked to mowers."""
    OTHER = "other"
    """Anything else (a pool cleaner, ...): not registrable."""


class DiscoveredDevice(ContractModel):
    """One device of a vendor cloud account as the discovery saw it."""

    @model_validator(mode="before")
    @classmethod
    def _device_type_from_is_mower(cls, data: Any) -> Any:
        """Lists cached before ``device_type`` existed carry only ``is_mower``: a mower stays a
        mower, anything else becomes ``other`` until the account is read again."""
        if isinstance(data, dict) and data.get("device_type") is None:
            return {**data, "device_type": "mower" if data.get("is_mower") else "other"}
        return data

    device_name: str = Field(
        min_length=1,
        max_length=100,
        description="The device name the vendor app shows; the key a "
        "mower registration selects the device by.",
    )
    iot_id: str | None = Field(default=None, max_length=100, description="Vendor IoT id.")
    product_key: str | None = Field(default=None, max_length=64)
    model: str | None = Field(
        default=None,
        max_length=100,
        description="Model name as far as the vendor cloud reported it at login.",
    )
    online: bool | None = Field(
        default=None, description="Online flag the cloud reported at login; null when unknown."
    )
    is_mower: bool = Field(
        description="False for RTK base stations, pool cleaners and other non-mowers, which "
        "cannot be registered as mowers."
    )
    device_type: DeviceType = Field(
        default=DeviceType.OTHER,
        description="`mower`, `rtk_base` (registered as a base station, ADR 0047) or `other`; "
        "derived from `is_mower` when a sender does not set it.",
    )


class CloudDiscoverRequest(ContractModel):
    """Request on ``gateway.cloud.discover`` (internal, Fleet to Device-Gateway)."""

    provider: str = Field(pattern=PROVIDER_ID_PATTERN, description="The provider id.")
    account_id: UUID = Field(description="The MowDeck account that links the vendor account.")
    linked_account_id: UUID | None = Field(
        default=None, description="Set for a re-read of a stored linked account; null on create."
    )
    credentials: dict[str, str] = Field(
        default_factory=dict,
        repr=False,
        max_length=10,
        description="Every credential field of the provider's descriptor by key (secrets "
        "included).",
    )
    email: str | None = Field(
        default=None,
        min_length=3,
        max_length=254,
        description="Deprecated (ADR 0034 senders): the identity field when `credentials` is "
        "empty.",
    )
    password: str | None = Field(
        default=None,
        repr=False,
        min_length=1,
        max_length=256,
        description="Deprecated (ADR 0034 senders): the only secret field when `credentials` is "
        "empty.",
    )

    @model_validator(mode="after")
    def _some_credentials(self) -> Self:
        if not self.credentials and (self.email is None or self.password is None):
            msg = "credentials (or the deprecated email and password) are required"
            raise ValueError(msg)
        if any(len(v) > MAX_CREDENTIAL_LENGTH for v in self.credentials.values()):
            msg = f"credential values are at most {MAX_CREDENTIAL_LENGTH} characters"
            raise ValueError(msg)
        return self


class CloudDiscoverErrorCode(StrEnum):
    """Why no device list was given."""

    LOGIN_FAILED = "login_failed"
    """The vendor cloud refused the credentials."""
    UNAVAILABLE = "unavailable"
    """The vendor cloud did not answer or failed (try again later)."""
    UNSUPPORTED = "unsupported"
    """The gateway has no adapter for the provider, or the provider is not enabled."""


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
