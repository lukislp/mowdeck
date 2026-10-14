"""In-process event bus with the same semantics as :class:`JetStreamBus`, for tests.

Messages are delivered synchronously inside ``publish`` (handlers run before ``publish``
returns), which keeps tests deterministic. Every published message is recorded in
:attr:`InMemoryBus.published`.
"""

import asyncio
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

from mowdeck_contracts.v1.subjects import STREAMS, StreamSpec, subject_matches
from pydantic import BaseModel

from mowdeck_service_kit.bus.base import (
    BusMessage,
    BusNotConnectedError,
    MessageHandler,
    NoRespondersError,
    PublishResult,
    RequestTimeoutError,
    dispatch,
    encode_payload,
    outgoing_headers,
)


@dataclass(eq=False)
class _MemorySubscription:
    bus: "InMemoryBus"
    subject: str
    handler: MessageHandler
    group: str | None
    jetstream: bool
    active: bool = field(default=True)

    async def unsubscribe(self) -> None:
        self.active = False
        self.bus._remove(self)


def _stream_for(streams: Iterable[StreamSpec], subject: str) -> StreamSpec | None:
    return next((s for s in streams if s.captures(subject)), None)


class InMemoryBus:
    """An :class:`~mowdeck_service_kit.bus.EventBus` that never leaves the process."""

    def __init__(
        self,
        *,
        max_deliver: int = 5,
        streams: Iterable[StreamSpec] = STREAMS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_deliver = max_deliver
        self.streams = tuple(streams)
        self.published: list[BusMessage] = []
        """Every message accepted by :meth:`publish` (duplicates excluded), in order."""
        self.dropped: list[BusMessage] = []
        """Messages terminated by a handler or that exhausted ``max_deliver``."""
        self._clock = clock
        self._subscriptions: list[_MemorySubscription] = []
        self._seen: dict[tuple[str, str], tuple[float, int]] = {}
        self._sequences: dict[str, int] = {}
        self._round_robin: dict[tuple[str, str], int] = {}
        self._last: dict[str, BusMessage] = {}
        """The newest stored message of every stream subject (``last_per_subject``)."""
        self._closed = False

    @property
    def is_connected(self) -> bool:
        """True until :meth:`close` is called."""
        return not self._closed

    def _ensure_open(self) -> None:
        if self._closed:
            msg = "in-memory bus is closed"
            raise BusNotConnectedError(msg)

    def _remove(self, subscription: _MemorySubscription) -> None:
        if subscription in self._subscriptions:
            self._subscriptions.remove(subscription)

    def _is_duplicate(self, stream: StreamSpec, msg_id: str) -> tuple[bool, int | None]:
        now = self._clock()
        window = stream.duplicate_window_seconds
        self._seen = {k: v for k, v in self._seen.items() if now - v[0] < window}
        seen = self._seen.get((stream.name, msg_id))
        return (True, seen[1]) if seen else (False, None)

    def _targets(self, subject: str, jetstream: bool) -> list[_MemorySubscription]:
        matching = [
            s
            for s in self._subscriptions
            if s.active and s.jetstream == jetstream and subject_matches(s.subject, subject)
        ]
        targets = [s for s in matching if s.group is None]
        groups: dict[str, list[_MemorySubscription]] = {}
        for sub in matching:
            if sub.group is not None:
                groups.setdefault(sub.group, []).append(sub)
        for group, members in groups.items():
            key = (subject, group)
            index = self._round_robin.get(key, 0)
            self._round_robin[key] = index + 1
            targets.append(members[index % len(members)])
        return targets

    async def publish(
        self,
        subject: str,
        payload: bytes | BaseModel,
        *,
        msg_id: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> PublishResult:
        """Publish and deliver synchronously; see :mod:`mowdeck_service_kit.bus.base`."""
        self._ensure_open()
        stream = _stream_for(self.streams, subject)
        message = BusMessage(
            subject=subject,
            data=encode_payload(payload),
            headers=outgoing_headers(payload, headers, msg_id),
        )
        if stream is None:
            self.published.append(message)
            for sub in self._targets(subject, jetstream=False):
                await dispatch(sub.handler, message)
            return PublishResult()

        if msg_id is not None:
            duplicate, sequence = self._is_duplicate(stream, msg_id)
            if duplicate:
                return PublishResult(duplicate=True, stream=stream.name, sequence=sequence)
        sequence = self._sequences.get(stream.name, 0) + 1
        self._sequences[stream.name] = sequence
        if msg_id is not None:
            self._seen[(stream.name, msg_id)] = (self._clock(), sequence)
        self.published.append(message)
        self._last[subject] = message
        for sub in self._targets(subject, jetstream=True):
            await self._deliver_with_retries(sub, message)
        return PublishResult(stream=stream.name, sequence=sequence)

    async def _deliver_with_retries(self, sub: _MemorySubscription, message: BusMessage) -> None:
        for attempt in range(1, self.max_deliver + 1):
            delivery = BusMessage(
                subject=message.subject,
                data=message.data,
                headers=dict(message.headers),
                delivery_attempt=attempt,
            )
            outcome = await dispatch(sub.handler, delivery)
            if outcome == "ack":
                return
            if outcome == "term":
                break
        self.dropped.append(message)

    async def subscribe(
        self,
        subject: str,
        handler: MessageHandler,
        *,
        durable: str | None = None,
        queue: str | None = None,
        last_per_subject: bool = False,
    ) -> _MemorySubscription:
        """Subscribe ``handler``; ``durable``/``queue`` form a load-balanced group.

        With ``last_per_subject`` the newest stored message of every matching stream subject is
        delivered to the new subscription before ``subscribe`` returns.
        """
        self._ensure_open()
        jetstream = _stream_for(self.streams, subject) is not None
        if (durable is not None or last_per_subject) and not jetstream:
            msg = f"durable and last-per-subject consumers need a stream subject: {subject!r}"
            raise ValueError(msg)
        sub = _MemorySubscription(
            bus=self,
            subject=subject,
            handler=handler,
            group=queue or durable,
            jetstream=jetstream,
        )
        self._subscriptions.append(sub)
        if last_per_subject:
            for stored_subject, message in list(self._last.items()):
                if subject_matches(subject, stored_subject):
                    await self._deliver_with_retries(sub, message)
        return sub

    async def request(
        self,
        subject: str,
        payload: bytes | BaseModel,
        timeout: float = 2.0,  # noqa: ASYNC109 - per-request deadline is part of the API
        *,
        headers: Mapping[str, str] | None = None,
    ) -> BusMessage:
        """Deliver to one core subscriber and wait for its :meth:`BusMessage.respond`."""
        self._ensure_open()
        if _stream_for(self.streams, subject) is not None:
            msg = f"request/reply uses core NATS subjects, {subject!r} belongs to a stream"
            raise ValueError(msg)
        targets = self._targets(subject, jetstream=False)
        if not targets:
            msg = f"no responders for {subject!r}"
            raise NoRespondersError(msg)
        reply: asyncio.Future[BusMessage] = asyncio.get_running_loop().create_future()

        async def responder(data: bytes, reply_headers: Mapping[str, str] | None) -> None:
            if not reply.done():
                reply.set_result(
                    BusMessage(
                        subject=f"_INBOX.{subject}", data=data, headers=dict(reply_headers or {})
                    )
                )

        message = BusMessage(
            subject=subject,
            data=encode_payload(payload),
            headers=outgoing_headers(payload, headers, None),
            _responder=responder,
        )
        self.published.append(message)
        task = asyncio.create_task(dispatch(targets[0].handler, message))
        try:
            async with asyncio.timeout(timeout):
                return await reply
        except TimeoutError as exc:
            msg = f"request to {subject!r} timed out after {timeout}s"
            raise RequestTimeoutError(msg) from exc
        finally:
            if not task.done():
                task.cancel()

    async def close(self) -> None:
        """Close the bus; later publishes raise :class:`BusNotConnectedError`. Idempotent."""
        self._closed = True
        self._subscriptions.clear()

    def messages(self, subject_pattern: str = ">") -> list[BusMessage]:
        """Published messages whose subject matches ``subject_pattern``."""
        return [m for m in self.published if subject_matches(subject_pattern, m.subject)]
