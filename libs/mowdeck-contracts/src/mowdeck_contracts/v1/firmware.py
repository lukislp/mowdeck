"""Mower firmware: what is installed, what the vendor offers, how an update goes (ADR 0038).

The Device-Gateway asks the vendor cloud for the firmware state of a mower (once after connecting,
then daily, and on request) and publishes it in
:attr:`~mowdeck_contracts.v1.MowerState.firmware_info`. Clients trigger a fresh check through
Fleet, which relays a :class:`FirmwareCheckRequest` on
:func:`~mowdeck_contracts.v1.subjects.firmware_check_subject` (core NATS request/reply,
internal) and gets a :class:`FirmwareCheckReply` from the gateway that holds the mower. The
update itself is the command :class:`~mowdeck_contracts.v1.FirmwareUpdate`; while it runs the
mower reports the activity ``updating`` and ``firmware_info.progress``.
"""

from enum import StrEnum
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from mowdeck_contracts.v1._base import ContractModel, UtcDateTime

FirmwareUpdateState = Literal["idle", "available", "installing", "failed", "unknown"]
"""Where the mower stands with its firmware: ``idle`` (up to date), ``available`` (a newer
version can be installed), ``installing`` (an update runs), ``failed`` (the last update failed,
see ``failure_reason``) or ``unknown`` (no check possible, e.g. a mower without cloud link)."""

FirmwareTarget = Literal["mower", "base_station"]
"""Whose firmware a check asks for: the mower's or its RTK base station's (ADR 0047)."""


class FirmwareInfo(ContractModel):
    """The firmware state of a mower (``MowerState.firmware_info``)."""

    installed: str | None = Field(
        default=None, max_length=64, description="The version running on the mower."
    )
    available: str | None = Field(
        default=None,
        max_length=64,
        description="The newest version the vendor offers for this mower; null when none or "
        "unknown.",
    )
    release_notes: str | None = Field(
        default=None, max_length=4000, description="Release notes of `available`, if given."
    )
    update_state: FirmwareUpdateState = "unknown"
    progress: int | None = Field(
        default=None, ge=0, le=100, description="Percent done while `installing`, if reported."
    )
    failure_reason: str | None = Field(
        default=None, max_length=500, description="Why the last update failed, if known."
    )
    checked_at: UtcDateTime | None = Field(
        default=None, description="When the vendor was last asked; null when never."
    )


class FirmwareCheckRequest(ContractModel):
    """Request on ``gateway.mower.<id>.firmware.check`` (internal, Fleet to Device-Gateway)."""

    mower_id: UUID
    account_id: UUID = Field(description="The mower's account as Fleet knows it.")
    target: FirmwareTarget = Field(
        default="mower",
        description="`mower`: the mower's own firmware; `base_station`: the firmware of the RTK "
        "base station the mower is linked to, asked through the mower's connection (ADR 0047).",
    )


class FirmwareCheckErrorCode(StrEnum):
    """Why no firmware information was given."""

    NOT_FOUND = "not_found"
    """The gateway does not hold the mower (or the shard owner is down)."""
    FORBIDDEN = "forbidden"
    """The account of the request does not own the mower."""
    UNSUPPORTED = "unsupported"
    """The mower's connection cannot ask the vendor (no cloud link)."""
    UNAVAILABLE = "unavailable"
    """The mower is not connected or the vendor cloud did not answer."""


class FirmwareCheckError(ContractModel):
    """The error of a :class:`FirmwareCheckReply`."""

    code: FirmwareCheckErrorCode
    detail: str = Field(max_length=500)


class FirmwareCheckReply(ContractModel):
    """Reply to :class:`FirmwareCheckRequest`: exactly one of ``info`` or ``error``."""

    info: FirmwareInfo | None = None
    error: FirmwareCheckError | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if (self.info is None) == (self.error is None):
            msg = "exactly one of info or error must be given"
            raise ValueError(msg)
        return self
