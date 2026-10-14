"""Event bus on NATS JetStream (nats-py).

On :meth:`JetStreamBus.connect` the streams from :data:`mowdeck_contracts.v1.subjects.STREAMS`
are created or updated. Stream subjects go through JetStream (persisted, de-duplicated by
``Nats-Msg-Id``, acknowledged); all other subjects use core NATS. The client reconnects forever;
connection state changes are logged and reflected in :attr:`JetStreamBus.is_connected`, which
backs the ``nats`` readiness check.
"""

import asyncio
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import nats
import nats.errors
import nats.js.errors
from mowdeck_contracts.v1.subjects import STREAMS, StreamSpec
from nats.aio.client import Client as NatsClient
from nats.aio.msg import Msg
from nats.aio.subscription import Subscription as NatsSubscription
from nats.js import JetStreamContext
from nats.js.api import (
    AckPolicy,
    ConsumerConfig,
    DeliverPolicy,
    DiscardPolicy,
    RetentionPolicy,
    StorageType,
    StreamConfig,
)
from pydantic import BaseModel

from mowdeck_service_kit.bus.base import (
    BusError,
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
from mowdeck_service_kit.logging import get_logger
from mowdeck_service_kit.settings import ServiceSettings

_log = get_logger(__name__)


def stream_config(spec: StreamSpec) -> StreamConfig:
    """Translate a contracts :class:`StreamSpec` into a nats-py :class:`StreamConfig`."""
    return StreamConfig(
        name=spec.name,
        description=spec.description,
        subjects=list(spec.subjects),
        retention=RetentionPolicy(spec.retention),
        storage=StorageType(spec.storage),
        max_age=float(spec.max_age_seconds),
        max_msgs_per_subject=spec.max_msgs_per_subject,
        max_bytes=spec.max_bytes,
        duplicate_window=float(spec.duplicate_window_seconds),
        discard=DiscardPolicy(spec.discard),
    )


@dataclass(eq=False)
class _NatsSubscriptionHandle:
    subscription: NatsSubscription

    async def unsubscribe(self) -> None:
        try:
            await self.subscription.unsubscribe()
        except nats.errors.Error as exc:  # already closed or draining
            _log.debug("unsubscribe ignored", error=repr(exc))


class JetStreamBus:
    """:class:`~mowdeck_service_kit.bus.EventBus` implementation on NATS JetStream."""

    def __init__(
        self,
        servers: str | Sequence[str],
        *,
        name: str | None = None,
        streams: Iterable[StreamSpec] = STREAMS,
        max_deliver: int = 5,
        ack_wait_seconds: float = 30.0,
        nak_delay_seconds: float = 1.0,
        connect_timeout_seconds: float = 2.0,
        publish_timeout_seconds: float = 5.0,
        drain_timeout_seconds: float = 10.0,
    ) -> None:
        self.servers = [servers] if isinstance(servers, str) else list(servers)
        self.name = name
        self.streams = tuple(streams)
        self.max_deliver = max_deliver
        self.ack_wait_seconds = ack_wait_seconds
        self.nak_delay_seconds = nak_delay_seconds
        self.connect_timeout_seconds = connect_timeout_seconds
        self.publish_timeout_seconds = publish_timeout_seconds
        self.drain_timeout_seconds = drain_timeout_seconds
        self._nc: NatsClient | None = None
        self._js: JetStreamContext | None = None

    @classmethod
    def from_settings(cls, settings: ServiceSettings, **options: Any) -> "JetStreamBus":
        """Create a bus for ``settings.nats_url`` named after the service."""
        return cls(settings.nats_servers, name=settings.service_name, **options)

    # --- connection -------------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        """True while the NATS connection is established."""
        return self._nc is not None and self._nc.is_connected

    async def connect(self) -> None:
        """Connect (retrying in the background after the first success) and ensure streams."""
        if self._nc is not None:
            return

        async def on_error(exc: Exception) -> None:
            _log.warning("nats error", error=repr(exc))

        async def on_disconnected() -> None:
            _log.warning("nats disconnected")

        async def on_reconnected() -> None:
            _log.info("nats reconnected")

        async def on_closed() -> None:
            _log.info("nats connection closed")

        self._nc = await nats.connect(
            servers=self.servers,
            name=self.name,
            connect_timeout=self.connect_timeout_seconds,
            allow_reconnect=True,
            max_reconnect_attempts=-1,
            reconnect_time_wait=2,
            error_cb=on_error,
            disconnected_cb=on_disconnected,
            reconnected_cb=on_reconnected,
            closed_cb=on_closed,
        )
        self._js = self._nc.jetstream()
        await self.ensure_streams()
        _log.info("nats connected", servers=self.servers)

    async def ensure_streams(self) -> None:
        """Create missing streams and update existing ones to the contract settings."""
        js = self._jetstream()
        for spec in self.streams:
            config = stream_config(spec)
            try:
                await js.stream_info(spec.name)
            except nats.js.errors.NotFoundError:
                await js.add_stream(config)
                _log.info("stream created", stream=spec.name)
                continue
            try:
                await js.update_stream(config)
            except nats.js.errors.APIError as exc:
                _log.warning("stream update rejected", stream=spec.name, error=repr(exc))

    def _client(self) -> NatsClient:
        if self._nc is None or self._nc.is_closed:
            msg = "JetStreamBus is not connected"
            raise BusNotConnectedError(msg)
        return self._nc

    def _jetstream(self) -> JetStreamContext:
        self._client()
        if self._js is None:  # pragma: no cover - set together with the client
            msg = "JetStreamBus is not connected"
            raise BusNotConnectedError(msg)
        return self._js

    def _stream_for(self, subject: str) -> StreamSpec | None:
        return next((s for s in self.streams if s.captures(subject)), None)

    async def close(self) -> None:
        """Drain subscriptions and pending publishes, then close. Idempotent."""
        nc, self._nc, self._js = self._nc, None, None
        if nc is None or nc.is_closed:
            return
        try:
            async with asyncio.timeout(self.drain_timeout_seconds):
                await nc.drain()
        except (TimeoutError, nats.errors.Error) as exc:
            _log.warning("nats drain failed, closing", error=repr(exc))
            await nc.close()

    # --- messaging --------------------------------------------------------------------------

    async def publish(
        self,
        subject: str,
        payload: bytes | BaseModel,
        *,
        msg_id: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> PublishResult:
        """Publish to JetStream (stream subjects) or core NATS (others)."""
        data = encode_payload(payload)
        all_headers = outgoing_headers(payload, headers, msg_id)
        stream = self._stream_for(subject)
        try:
            if stream is None:
                await self._client().publish(subject, data, headers=all_headers or None)
                return PublishResult()
            ack = await self._jetstream().publish(
                subject,
                data,
                timeout=self.publish_timeout_seconds,
                stream=stream.name,
                headers=all_headers,
            )
        except nats.errors.Error as exc:
            msg = f"publish to {subject!r} failed: {exc!r}"
            raise BusError(msg) from exc
        return PublishResult(duplicate=bool(ack.duplicate), stream=ack.stream, sequence=ack.seq)

    async def subscribe(
        self,
        subject: str,
        handler: MessageHandler,
        *,
        durable: str | None = None,
        queue: str | None = None,
        last_per_subject: bool = False,
    ) -> _NatsSubscriptionHandle:
        """Subscribe; stream subjects get a JetStream push consumer with manual acks."""
        stream = self._stream_for(subject)
        if stream is None:
            if durable is not None or last_per_subject:
                msg = f"durable and last-per-subject consumers need a stream subject: {subject!r}"
                raise ValueError(msg)
            return await self._subscribe_core(subject, handler, queue)
        return await self._subscribe_stream(
            stream, subject, handler, durable, queue, last_per_subject=last_per_subject
        )

    async def _subscribe_stream(
        self,
        stream: StreamSpec,
        subject: str,
        handler: MessageHandler,
        durable: str | None,
        queue: str | None,
        *,
        last_per_subject: bool = False,
    ) -> _NatsSubscriptionHandle:
        async def callback(msg: Msg) -> None:
            message = BusMessage(
                subject=msg.subject,
                data=msg.data,
                headers=dict(msg.headers or {}),
                delivery_attempt=msg.metadata.num_delivered or 1,
            )
            outcome = await dispatch(handler, message)
            try:
                if outcome == "ack":
                    await msg.ack()
                elif outcome == "term":
                    await msg.term()
                elif message.delivery_attempt >= self.max_deliver:
                    _log.error("message exhausted redeliveries", subject=msg.subject)
                    await msg.term()
                else:
                    await msg.nak(delay=self.nak_delay_seconds)
            except nats.errors.Error as exc:
                _log.warning("ack failed", subject=msg.subject, error=repr(exc))

        config = ConsumerConfig(
            ack_policy=AckPolicy.EXPLICIT,
            ack_wait=self.ack_wait_seconds,
            max_deliver=self.max_deliver,
            deliver_policy=(
                DeliverPolicy.LAST_PER_SUBJECT if last_per_subject else DeliverPolicy.NEW
            ),
        )
        subscription = await self._jetstream().subscribe(
            subject,
            queue=queue,
            cb=callback,
            durable=durable,
            stream=stream.name,
            config=config,
            manual_ack=True,
        )
        return _NatsSubscriptionHandle(subscription)

    async def _subscribe_core(
        self, subject: str, handler: MessageHandler, queue: str | None
    ) -> _NatsSubscriptionHandle:
        client = self._client()

        async def callback(msg: Msg) -> None:
            reply_to = msg.reply

            async def responder(data: bytes, headers: Mapping[str, str] | None) -> None:
                await client.publish(reply_to, data, headers=dict(headers) if headers else None)

            message = BusMessage(
                subject=msg.subject,
                data=msg.data,
                headers=dict(msg.headers or {}),
                _responder=responder if reply_to else None,
            )
            await dispatch(handler, message)

        subscription = await client.subscribe(subject, queue=queue or "", cb=callback)
        return _NatsSubscriptionHandle(subscription)

    async def request(
        self,
        subject: str,
        payload: bytes | BaseModel,
        timeout: float = 2.0,  # noqa: ASYNC109 - per-request deadline is part of the API
        *,
        headers: Mapping[str, str] | None = None,
    ) -> BusMessage:
        """Core NATS request/reply."""
        if self._stream_for(subject) is not None:
            msg = f"request/reply uses core NATS subjects, {subject!r} belongs to a stream"
            raise ValueError(msg)
        all_headers = outgoing_headers(payload, headers, None)
        try:
            reply = await self._client().request(
                subject, encode_payload(payload), timeout=timeout, headers=all_headers or None
            )
        except nats.errors.NoRespondersError as exc:
            msg = f"no responders for {subject!r}"
            raise NoRespondersError(msg) from exc
        except nats.errors.TimeoutError as exc:
            msg = f"request to {subject!r} timed out after {timeout}s"
            raise RequestTimeoutError(msg) from exc
        return BusMessage(subject=reply.subject, data=reply.data, headers=dict(reply.headers or {}))
