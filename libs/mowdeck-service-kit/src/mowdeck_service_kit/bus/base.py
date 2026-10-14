"""The event bus abstraction shared by the JetStream and in-memory implementations.

Delivery semantics (identical for both implementations):

* Subjects captured by a stream from :mod:`mowdeck_contracts.v1.subjects` (``command.>``,
  ``telemetry.>``, ``event.>``) are persisted in JetStream; any other subject uses core NATS
  (fire and forget, request/reply).
* ``msg_id`` de-duplicates: a second publish with the same id within the stream's duplicate
  window is dropped and reported as ``PublishResult.duplicate``.
* A handler that returns normally acknowledges the message. A handler that raises gets the
  message redelivered (nak) up to ``max_deliver`` times. Raising :class:`DropMessage`
  terminates the message without redelivery (use it for poison messages).
* Subscribers sharing a ``queue`` (or ``durable``) name form a group: each message goes to one
  member. Without a group every subscriber receives every message.
* New subscriptions start with new messages; they do not replay history.
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from mowdeck_contracts.v1 import CommandEnvelope, CommandResult, EventEnvelope
from mowdeck_contracts.v1.subjects import (
    command_result_subject,
    command_subject,
    event_subject_for_type,
)
from pydantic import BaseModel

from mowdeck_service_kit.correlation import (
    CORRELATION_HEADER,
    correlation_scope,
    get_correlation_id,
)
from mowdeck_service_kit.logging import get_logger

_log = get_logger(__name__)

MSG_ID_HEADER = "Nats-Msg-Id"
"""JetStream de-duplication header."""
CONTENT_TYPE_HEADER = "Content-Type"
EVENT_TYPE_HEADER = "Mowdeck-Event-Type"
EVENT_VERSION_HEADER = "Mowdeck-Event-Version"
PRIORITY_HEADER = "Mowdeck-Priority"
JSON_CONTENT_TYPE = "application/json"


class BusError(Exception):
    """Base class of event bus errors."""


class BusNotConnectedError(BusError):
    """The bus is not connected (or already closed)."""


class NoRespondersError(BusError):
    """A request found no subscriber to answer it."""


class RequestTimeoutError(BusError):
    """A request was not answered in time."""


class DropMessage(Exception):  # noqa: N818 - reads as an instruction in handler code
    """Raise from a handler to discard the message without redelivery."""


Responder = Callable[[bytes, Mapping[str, str] | None], Awaitable[None]]


@dataclass(slots=True)
class BusMessage:
    """A message delivered to a handler, or the reply to a request."""

    subject: str
    data: bytes
    headers: dict[str, str] = field(default_factory=dict)
    delivery_attempt: int = 1
    _responder: Responder | None = field(default=None, repr=False)

    @property
    def msg_id(self) -> str | None:
        """The ``Nats-Msg-Id`` header, if set."""
        return self.headers.get(MSG_ID_HEADER)

    @property
    def correlation_id(self) -> str | None:
        """The correlation id header, if set."""
        return self.headers.get(CORRELATION_HEADER)

    def model[ModelT: BaseModel](self, model: type[ModelT]) -> ModelT:
        """Decode the JSON body into ``model`` (raises ``pydantic.ValidationError``)."""
        return model.model_validate_json(self.data)

    async def respond(
        self, payload: bytes | BaseModel, headers: Mapping[str, str] | None = None
    ) -> None:
        """Answer a request. Raises :class:`BusError` if the message expects no reply."""
        if self._responder is None:
            msg = f"message on {self.subject!r} has no reply subject"
            raise BusError(msg)
        await self._responder(encode_payload(payload), headers)


@dataclass(frozen=True, slots=True)
class PublishResult:
    """Outcome of a publish."""

    duplicate: bool = False
    """True if JetStream dropped the message as a duplicate ``msg_id``."""
    stream: str | None = None
    """Stream that stored the message; None for core NATS subjects."""
    sequence: int | None = None
    """Stream sequence number, if stored."""


MessageHandler = Callable[[BusMessage], Awaitable[None]]


@runtime_checkable
class Subscription(Protocol):
    """Handle of an active subscription."""

    async def unsubscribe(self) -> None:
        """Stop receiving messages. Durable consumers keep their position on the server."""
        ...


@runtime_checkable
class EventBus(Protocol):
    """Publish/subscribe and request/reply over NATS."""

    async def publish(
        self,
        subject: str,
        payload: bytes | BaseModel,
        *,
        msg_id: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> PublishResult:
        """Publish ``payload`` (models are sent as JSON) to ``subject``."""
        ...

    async def subscribe(
        self,
        subject: str,
        handler: MessageHandler,
        *,
        durable: str | None = None,
        queue: str | None = None,
        last_per_subject: bool = False,
    ) -> Subscription:
        """Call ``handler`` for every message on ``subject`` (wildcards allowed).

        Stream subjects deliver new messages only; with ``last_per_subject`` a new consumer
        starts with the newest stored message of every matching subject (JetStream
        ``DeliverPolicy.LAST_PER_SUBJECT``), for last-value streams such as ``DEVICE``.
        """
        ...

    async def request(
        self,
        subject: str,
        payload: bytes | BaseModel,
        timeout: float = 2.0,  # noqa: ASYNC109 - per-request deadline is part of the API
        *,
        headers: Mapping[str, str] | None = None,
    ) -> BusMessage:
        """Send a core NATS request and wait for the first reply."""
        ...

    async def close(self) -> None:
        """Drain in-flight messages and close the connection. Idempotent."""
        ...

    @property
    def is_connected(self) -> bool:
        """True while the bus can publish."""
        ...


Outcome = Literal["ack", "nak", "term"]


async def dispatch(handler: MessageHandler, message: BusMessage) -> Outcome:
    """Run ``handler`` in the message's correlation scope and map the result to an outcome.

    Returns ``ack`` on success, ``term`` for :class:`DropMessage` and ``nak`` for any other
    exception (which is logged).
    """
    with correlation_scope(message.correlation_id):
        try:
            await handler(message)
        except DropMessage as exc:
            _log.warning("message dropped by handler", subject=message.subject, reason=str(exc))
            return "term"
        except Exception:
            _log.exception(
                "message handler failed",
                subject=message.subject,
                delivery_attempt=message.delivery_attempt,
            )
            return "nak"
    return "ack"


def encode_payload(payload: bytes | BaseModel) -> bytes:
    """Encode a payload: bytes pass through, Pydantic models become JSON."""
    if isinstance(payload, bytes):
        return payload
    return payload.model_dump_json().encode("utf-8")


def outgoing_headers(
    payload: bytes | BaseModel,
    headers: Mapping[str, str] | None,
    msg_id: str | None,
) -> dict[str, str]:
    """Headers for an outgoing message: content type, msg id and correlation id."""
    result: dict[str, str] = {}
    if isinstance(payload, BaseModel):
        result[CONTENT_TYPE_HEADER] = JSON_CONTENT_TYPE
    correlation_id = get_correlation_id()
    if correlation_id is not None:
        result[CORRELATION_HEADER] = correlation_id
    result.update(headers or {})
    if msg_id is not None:
        result[MSG_ID_HEADER] = msg_id
    return result


async def publish_event(bus: EventBus, envelope: EventEnvelope[Any]) -> PublishResult:
    """Publish a domain event on ``event.<domain>.<name>`` with ``msg_id = envelope.id``."""
    return await bus.publish(
        event_subject_for_type(envelope.type),
        envelope,
        msg_id=str(envelope.id),
        headers={
            CORRELATION_HEADER: envelope.correlation_id,
            EVENT_TYPE_HEADER: envelope.type,
            EVENT_VERSION_HEADER: str(envelope.version),
        },
    )


async def publish_command(bus: EventBus, envelope: CommandEnvelope) -> PublishResult:
    """Publish a command on ``command.mower.<id>`` with ``msg_id = command_id``.

    Raises ``ValueError`` if the command has already expired.
    """
    if envelope.is_expired():
        msg = f"command {envelope.command_id} expired at {envelope.expires_at.isoformat()}"
        raise ValueError(msg)
    return await bus.publish(
        command_subject(envelope.mower_id),
        envelope,
        msg_id=str(envelope.command_id),
        headers={
            CORRELATION_HEADER: envelope.correlation_id,
            PRIORITY_HEADER: envelope.priority.value,
        },
    )


async def publish_command_result(bus: EventBus, result: CommandResult) -> PublishResult:
    """Publish a command result on ``command.result.mower.<id>``.

    The msg id combines command id and status, so each status transition is stored once.
    """
    headers = {CORRELATION_HEADER: result.correlation_id} if result.correlation_id else None
    return await bus.publish(
        command_result_subject(result.mower_id),
        result,
        msg_id=f"{result.command_id}:{result.status.value}",
        headers=headers,
    )
