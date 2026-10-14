"""The command API of Fleet (``/api/v1/fleet``), shared with clients (web app, Home Assistant).

A client sends a :class:`CommandRequest` for one mower (or for every mower of a site or group).
Fleet checks the caller's right for the kind of command, builds the
:class:`~mowdeck_contracts.v1.CommandEnvelope` from the verified principal, keeps a command
record and answers with :class:`CommandAccepted` (one mower) or :class:`BulkCommandAccepted`
(site or group). The record follows the results the Device-Gateway publishes; clients read it
as :class:`CommandView`.

Status of a record (:class:`CommandState`): ``pending`` (published, no result yet), then
``accepted`` (the gateway admitted it), then one final status: ``executed``, ``rejected``,
``expired`` or ``failed``. A later result never moves a record backwards.
"""

from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from mowdeck_contracts.v1._base import ContractModel, CorrelationId, UtcDateTime
from mowdeck_contracts.v1.envelopes import CommandPriority
from mowdeck_contracts.v1.mower import MowerCommand
from mowdeck_contracts.v1.principal import PrincipalRef


class CommandRequest(ContractModel):
    """Body of ``POST .../commands`` for a mower, a group or a site.

    ``ttl_seconds`` shortens or extends the expiry within the service's limit; Fleet ignores it
    for ``manual_drive`` and ``emergency_stop``, which always use their own short expiry.
    """

    command: MowerCommand
    ttl_seconds: int | None = Field(default=None, ge=1, le=3600)


class CommandState(StrEnum):
    """Status of a command record in Fleet."""

    PENDING = "pending"
    """Published on the bus, no result yet."""
    ACCEPTED = "accepted"
    """The Device-Gateway admitted it."""
    REJECTED = "rejected"
    """Refused before execution."""
    EXECUTED = "executed"
    """The mower confirmed execution."""
    EXPIRED = "expired"
    """Dropped because it expired."""
    FAILED = "failed"
    """Execution or publishing failed."""


FINAL_COMMAND_STATES: frozenset[CommandState] = frozenset(
    {CommandState.REJECTED, CommandState.EXECUTED, CommandState.EXPIRED, CommandState.FAILED}
)
"""Statuses after which nothing changes any more."""


class CommandAccepted(ContractModel):
    """Answer (202) to a command for one mower: Fleet published it; results follow."""

    command_id: UUID
    mower_id: UUID
    status: Literal["accepted"] = "accepted"
    """The command was accepted for delivery (the record itself starts as ``pending``)."""
    kind: str = Field(min_length=1, max_length=64)
    priority: CommandPriority
    issued_at: UtcDateTime
    expires_at: UtcDateTime
    correlation_id: CorrelationId
    status_url: str = Field(
        description="Where to read the command record, e.g. `/api/v1/fleet/commands/<id>`."
    )


class CommandView(ContractModel):
    """A command record as clients read it (``GET /commands/{id}``)."""

    command_id: UUID
    mower_id: UUID
    bulk_id: UUID | None = None
    kind: str = Field(min_length=1, max_length=64)
    priority: CommandPriority
    status: CommandState
    reason: str | None = Field(default=None, max_length=500)
    issued_by: PrincipalRef
    issued_at: UtcDateTime
    expires_at: UtcDateTime
    result_at: UtcDateTime | None = Field(
        default=None, description="Time of the latest result applied to the record."
    )
    correlation_id: CorrelationId
    origin: str | None = Field(
        default=None,
        max_length=32,
        description="Service that issued the command on behalf of `issued_by` (e.g. "
        "`scheduler`); None for commands sent through the API.",
    )
    schedule_id: UUID | None = Field(
        default=None, description="The schedule that issued the command, if any."
    )
    plan_hash: str | None = Field(
        default=None,
        max_length=64,
        description="Hash of the spot plan of a `start_mow` with `spot_plan`, if any.",
    )
    end_job: bool | None = Field(
        default=None,
        description="For a `return_to_dock`: whether it also ended the job; None for other kinds.",
    )
    pattern: dict[str, Any] | None = Field(
        default=None,
        description="For a `start_mow` on map areas: the pattern settings it was sent with "
        "(`StartMow.pattern`, mostly `MowPattern` keys); None otherwise or when it had none.",
    )


class BulkCommandOutcome(StrEnum):
    """What happened to one mower of a bulk command."""

    ACCEPTED = "accepted"
    """Published for this mower; follow ``command_id``."""
    FORBIDDEN = "forbidden"
    """The caller sees the mower but may not send it this command."""
    NOT_PUBLISHED = "not_published"
    """Allowed, but the bus was not reachable; the record is ``failed``."""


class BulkCommandItem(ContractModel):
    """One mower of a bulk command. Mowers the caller cannot see are omitted."""

    mower_id: UUID
    outcome: BulkCommandOutcome
    command_id: UUID | None = None
    expires_at: UtcDateTime | None = None


class BulkCommandAccepted(ContractModel):
    """Answer (202) to a command for every mower of a site or group."""

    bulk_id: UUID
    target_type: Literal["site", "group"]
    target_id: UUID
    kind: str = Field(min_length=1, max_length=64)
    items: list[BulkCommandItem] = Field(default_factory=list)
