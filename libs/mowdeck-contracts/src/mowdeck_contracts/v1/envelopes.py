"""Envelopes that wrap every message on the event bus.

* :class:`EventEnvelope` for domain events (``event.<domain>.<name>``).
* :class:`CommandEnvelope` for mower commands (``command.mower.<id>``).
* :class:`CommandResult` for the outcome of a command (``command.result.mower.<id>``).

The envelope id (``EventEnvelope.id`` or ``CommandEnvelope.command_id``) is also used as the
JetStream ``Nats-Msg-Id`` header, so a message published twice within the stream's duplicate
window is stored only once.
"""

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Self
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from mowdeck_contracts.v1._base import (
    ContractModel,
    CorrelationId,
    DottedKey,
    UtcDateTime,
    utc_now,
)
from mowdeck_contracts.v1.mower import MowerCommand
from mowdeck_contracts.v1.principal import PrincipalRef


def _new_correlation_id() -> str:
    return str(uuid4())


def _require_aware(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        msg = "now must be timezone-aware"
        raise ValueError(msg)
    return now


class EventEnvelope[PayloadT](ContractModel):
    """A domain event such as ``mower.registered``.

    ``PayloadT`` is the typed payload model. Use ``EventEnvelope[dict[str, Any]]`` when the
    payload type is not known (for example in generic consumers).

    Versioning: ``type`` plus ``version`` identify the payload schema. Within one version only
    backward compatible changes are allowed (new optional fields). A changed meaning gets a new
    ``version``; an event is never reinterpreted.
    """

    id: UUID = Field(default_factory=uuid4, description="Unique event id; JetStream msg id.")
    type: DottedKey = Field(description="Event type `<domain>.<name>`, e.g. `mower.registered`.")
    version: int = Field(default=1, ge=1, description="Payload schema version of this type.")
    occurred_at: UtcDateTime = Field(default_factory=utc_now)
    account_id: UUID | None = Field(
        default=None, description="Owning account; None only for platform level events."
    )
    correlation_id: CorrelationId = Field(default_factory=_new_correlation_id)
    causation_id: UUID | None = Field(
        default=None, description="Id of the event or command that caused this event."
    )
    producer: str = Field(min_length=1, max_length=64, description="Name of the producing service.")
    payload: PayloadT

    @property
    def domain(self) -> str:
        """The domain part of :attr:`type` (before the first dot)."""
        return self.type.split(".", 1)[0]

    @property
    def name(self) -> str:
        """The name part of :attr:`type` (after the first dot)."""
        return self.type.split(".", 1)[1]


class CommandPriority(StrEnum):
    """Delivery priority of a command. Only :class:`EmergencyStop` uses ``emergency``."""

    NORMAL = "normal"
    EMERGENCY = "emergency"


class CommandEnvelope(ContractModel):
    """A command for exactly one mower.

    Rules enforced on validation:

    * ``expires_at`` is after ``issued_at``; consumers drop expired commands (see
      :meth:`is_expired`), so a stale "start" is never executed.
    * ``priority`` is ``emergency`` if and only if the command is an emergency stop. When
      ``priority`` is omitted it is derived from the command.
    """

    command_id: UUID = Field(default_factory=uuid4, description="Unique id; JetStream msg id.")
    mower_id: UUID
    account_id: UUID
    issued_by: PrincipalRef
    issued_at: UtcDateTime = Field(default_factory=utc_now)
    expires_at: UtcDateTime
    priority: CommandPriority
    correlation_id: CorrelationId = Field(default_factory=_new_correlation_id)
    command: MowerCommand

    @model_validator(mode="before")
    @classmethod
    def _derive_priority(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("priority") is None:
            command = data.get("command")
            kind = (
                command.get("kind") if isinstance(command, dict) else getattr(command, "kind", None)
            )
            priority = (
                CommandPriority.EMERGENCY if kind == "emergency_stop" else CommandPriority.NORMAL
            )
            return {**data, "priority": priority}
        return data

    @model_validator(mode="after")
    def _check_rules(self) -> Self:
        if self.expires_at <= self.issued_at:
            msg = "expires_at must be after issued_at"
            raise ValueError(msg)
        is_emergency_stop = self.command.kind == "emergency_stop"
        if is_emergency_stop != (self.priority is CommandPriority.EMERGENCY):
            msg = "priority must be 'emergency' for emergency_stop and only for emergency_stop"
            raise ValueError(msg)
        return self

    @classmethod
    def create(
        cls,
        *,
        mower_id: UUID,
        account_id: UUID,
        issued_by: PrincipalRef,
        command: MowerCommand,
        ttl: timedelta = timedelta(seconds=30),
        correlation_id: str | None = None,
        now: datetime | None = None,
    ) -> Self:
        """Build an envelope that expires ``ttl`` after ``now`` (default: current UTC time)."""
        issued_at = _require_aware(now) if now is not None else utc_now()
        data: dict[str, Any] = {
            "mower_id": mower_id,
            "account_id": account_id,
            "issued_by": issued_by,
            "issued_at": issued_at,
            "expires_at": issued_at + ttl,
            "command": command,
        }
        if correlation_id is not None:
            data["correlation_id"] = correlation_id
        return cls.model_validate(data)

    def is_expired(self, now: datetime | None = None) -> bool:
        """Return True if the command must no longer be executed at ``now`` (default: now)."""
        current = _require_aware(now) if now is not None else utc_now()
        return current >= self.expires_at


class CommandStatus(StrEnum):
    """Outcome of a command."""

    ACCEPTED = "accepted"
    """Received and forwarded to the mower."""
    REJECTED = "rejected"
    """Refused before execution (validation, permission, device limits)."""
    EXECUTED = "executed"
    """The mower confirmed execution."""
    EXPIRED = "expired"
    """Dropped because ``expires_at`` had passed."""
    FAILED = "failed"
    """Execution was attempted and failed."""


class CommandResult(ContractModel):
    """The outcome of a command, published by the Device-Gateway."""

    command_id: UUID
    mower_id: UUID
    status: CommandStatus
    reason: str | None = Field(default=None, max_length=500)
    at: UtcDateTime = Field(default_factory=utc_now)
    correlation_id: CorrelationId | None = Field(
        default=None, description="Correlation id of the command, if known."
    )
