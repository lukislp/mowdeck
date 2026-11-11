# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: Apache-2.0

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
* A batch subscription (:meth:`EventBus.subscribe_batch`, stream subjects only) hands the
  handler every message that arrived while the previous batch was being handled (up to
  ``max_batch``, optionally lingering ``max_wait_seconds`` for more), in delivery order. The
  handler answers one outcome per message; each message is acknowledged only after the handler
  returned, so a crash loses nothing (JetStream redelivers what was not acknowledged). Raising
  from a batch handler naks the whole batch. Unsubscribing settles the messages already
  received before it returns.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
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

Outcome = Literal["ack", "nak", "term"]
"""What happens to a delivered message: acknowledged, redelivered later, or discarded."""

BatchHandler = Callable[[list[BusMessage]], Awaitable[Sequence[Outcome]]]
"""Handles a batch of messages and answers one :data:`Outcome` per message, in order."""


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
        acknowledge: bool = True,
    ) -> Subscription:
        """Call ``handler`` for every message on ``subject`` (wildcards allowed).

        Stream subjects deliver new messages only; with ``last_per_subject`` a new consumer
        starts with the newest stored message of every matching subject (JetStream
        ``DeliverPolicy.LAST_PER_SUBJECT``), for last-value streams such as ``DEVICE``.
        ``acknowledge=False`` (only without ``durable`` and ``queue``) is for live views of a
        stream, such as a fan-out to sockets or a "latest value" cache: the server sends each
        message once and expects no acknowledgement (JetStream ``AckPolicy.NONE``), so a failing
        handler gets no redelivery, and the server and the client save one message per
        delivery (ADR 0078).
        """
        ...

    async def subscribe_batch(
        self,
        subject: str,
        handler: BatchHandler,
        *,
        durable: str | None = None,
        queue: str | None = None,
        max_batch: int = 64,
        max_wait_seconds: float = 0.0,
    ) -> Subscription:
        """Call ``handler`` with batches of the messages on a stream ``subject`` (see the module
        documentation); ``durable``/``queue`` as for :meth:`subscribe`."""
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


@dataclass(frozen=True, slots=True)
class StoredMessage:
    """A message as a stream holds it (:meth:`StreamStore.stored_messages`)."""

    stream: str
    sequence: int
    subject: str
    data: bytes
    headers: Mapping[str, str] = field(default_factory=dict)


@runtime_checkable
class StreamStore(Protocol):
    """What the streams *hold*, for the one purpose of removing it (ADR 0082).

    An :class:`EventBus` moves messages; the streams also keep them for their retention (30 days
    in ``EVENTS`` and ``DEVICE``), so data of an erased account or a removed mower would outlive
    its rows there. This port removes it. It is deliberately not part of :class:`EventBus`: only
    the service that owns what a message is about may hold it (Fleet for the subjects of a mower,
    Identity for the events of an account or a user), and the NATS permissions allow the calls to
    nobody else (``tools/repo-checks/scripts/nats_auth.py``, ``purge`` and ``erase``).

    Two ways, by what the subject can address:

    * :meth:`purge_subject` removes every message of a subject filter in one server call. For
      subjects that name what is removed (``telemetry.mower.<id>``).
    * :meth:`stored_messages` and :meth:`erase_message` read a subject filter message by message
      and remove single ones. For subjects that do not name it (``event.<domain>.<name>``: the
      account is in the envelope), where the caller decides per message.

    Every call is idempotent: purging or erasing what is gone removes nothing. All subjects must
    belong to a stream (``ValueError`` otherwise).
    """

    async def purge_subject(self, subject: str) -> int:
        """Remove every stored message matching ``subject`` (wildcards allowed) from its stream;
        returns how many were removed."""
        ...

    async def count_subject(self, subject: str) -> int:
        """How many stored messages match ``subject`` (a cheap server side count)."""
        ...

    def stored_messages(self, subject: str) -> AsyncIterator[StoredMessage]:
        """The stored messages matching ``subject``, oldest first, as they are now (messages
        published while reading may or may not be seen)."""
        ...

    async def erase_message(self, message: StoredMessage) -> bool:
        """Remove one stored message (overwritten, not only unlinked); False when it was gone."""
        ...


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


async def dispatch_batch(handler: BatchHandler, messages: list[BusMessage]) -> list[Outcome]:
    """Run a batch handler; one outcome per message.

    :class:`DropMessage` from the handler terminates the whole batch, any other exception naks
    it (logged). A handler answering the wrong number of outcomes naks the batch too.
    """
    try:
        outcomes = list(await handler(messages))
    except DropMessage as exc:
        _log.warning(
            "batch dropped by handler",
            subject=messages[0].subject,
            size=len(messages),
            reason=str(exc),
        )
        return ["term"] * len(messages)
    except Exception:
        _log.exception("batch handler failed", subject=messages[0].subject, size=len(messages))
        return ["nak"] * len(messages)
    if len(outcomes) != len(messages):
        _log.error(
            "batch handler answered the wrong number of outcomes",
            subject=messages[0].subject,
            size=len(messages),
            outcomes=len(outcomes),
        )
        return ["nak"] * len(messages)
    return outcomes


def check_acknowledge(acknowledge: bool, durable: str | None, queue: str | None) -> None:
    """Raise ``ValueError`` for an unacknowledged durable or queue consumer: shared consumers
    distribute work, and work must not get lost when a handler fails."""
    if not acknowledge and (durable is not None or queue is not None):
        msg = "acknowledge=False is for live views only, not for durable or queue consumers"
        raise ValueError(msg)


def check_batch_limits(max_batch: int, max_wait_seconds: float) -> None:
    """Raise ``ValueError`` for a batch size below 1 or a negative or long linger (> 5 s)."""
    if max_batch < 1:
        msg = "max_batch must be at least 1"
        raise ValueError(msg)
    if not 0 <= max_wait_seconds <= MAX_BATCH_WAIT_SECONDS:
        msg = f"max_wait_seconds must be between 0 and {MAX_BATCH_WAIT_SECONDS:g}"
        raise ValueError(msg)


MAX_BATCH_WAIT_SECONDS = 5.0
"""Longest linger of a batch subscription (well below the acknowledgement wait)."""


class _Stop:
    """Marks the end of a :class:`BatchWorker`'s queue."""


_STOP = _Stop()


class BatchWorker[T]:
    """Collects delivered messages and hands them to a batch handler, one batch at a time.

    ``put`` (from the transport's delivery callback) never waits; the worker takes everything
    queued (up to ``max_batch``), lingers up to ``max_wait_seconds`` for more, runs the handler
    and then settles each message with its outcome, in delivery order. Batches never overlap,
    so messages of one subscription are handled in the order they arrived.
    :meth:`stop` lets the worker handle and settle everything queued before it, within a time
    limit; what is not settled then is redelivered by the server.
    """

    def __init__(
        self,
        handler: BatchHandler,
        *,
        to_message: Callable[[T], BusMessage],
        settle: Callable[[T, BusMessage, Outcome], Awaitable[None]],
        max_batch: int,
        max_wait_seconds: float,
        name: str,
    ) -> None:
        check_batch_limits(max_batch, max_wait_seconds)
        self._handler = handler
        self._to_message = to_message
        self._settle = settle
        self._max_batch = max_batch
        self._max_wait = max_wait_seconds
        self._name = name
        self._queue: asyncio.Queue[T | _Stop] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self.batches = 0
        """Batches handled so far (tests, metrics)."""

    def start(self) -> None:
        """Start the worker task (once)."""
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name=f"batch-{self._name}")

    def put(self, item: T) -> None:
        """Queue a delivered message."""
        self._queue.put_nowait(item)

    @property
    def queued(self) -> int:
        """Messages waiting for the next batch."""
        return self._queue.qsize()

    async def stop(self, timeout_seconds: float) -> None:
        """Handle and settle what was queued, then end the worker. Idempotent."""
        task = self._task
        if task is None or task.done():
            return
        self._queue.put_nowait(_STOP)
        try:
            async with asyncio.timeout(timeout_seconds):
                await asyncio.shield(task)
        except TimeoutError:
            _log.warning(
                "batch worker did not finish in time, the server redelivers the rest",
                subscription=self._name,
                queued=self._queue.qsize(),
            )
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _next(self, deadline: float) -> T | _Stop | None:
        """The next queued item, waiting until ``deadline`` at most; None when none came."""
        try:
            return self._queue.get_nowait()
        except asyncio.QueueEmpty:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return None
        try:
            async with asyncio.timeout(remaining):
                return await self._queue.get()
        except TimeoutError:
            return None

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            first = await self._queue.get()
            if isinstance(first, _Stop):
                return
            items: list[T] = [first]
            stopping = False
            deadline = loop.time() + self._max_wait
            while len(items) < self._max_batch:
                item = await self._next(deadline)
                if item is None:
                    break
                if isinstance(item, _Stop):
                    stopping = True
                    break
                items.append(item)
            await self._handle(items)
            if stopping:
                return

    async def _handle(self, items: list[T]) -> None:
        messages = [self._to_message(item) for item in items]
        outcomes = await dispatch_batch(self._handler, messages)
        self.batches += 1
        for item, message, outcome in zip(items, messages, outcomes, strict=True):
            try:
                await self._settle(item, message, outcome)
            except Exception as exc:
                _log.warning("settling a message failed", subject=message.subject, error=repr(exc))


class BatchNakError(Exception):
    """A batch handler asked for the redelivery of a message (the single-message adapter)."""


def one_at_a_time(handler: BatchHandler) -> MessageHandler:
    """A batch handler as a :data:`MessageHandler` that hands it batches of one message (the
    in-memory bus, which delivers synchronously inside ``publish``)."""

    async def handle(message: BusMessage) -> None:
        outcome = (await dispatch_batch(handler, [message]))[0]
        if outcome == "term":
            raise DropMessage("dropped by the batch handler")
        if outcome == "nak":
            raise BatchNakError("the batch handler asked for a redelivery")

    return handle


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


async def publish_event(
    bus: EventBus, envelope: EventEnvelope[Any], *, subject: str | None = None
) -> PublishResult:
    """Publish a domain event on ``event.<domain>.<name>`` with ``msg_id = envelope.id``.

    ``subject`` replaces the subject of the type for the rare event whose subject carries an id
    below it (``event.identity.access.revoked.<id>``, so the newest one per id can be read back).
    """
    return await bus.publish(
        subject or event_subject_for_type(envelope.type),
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
