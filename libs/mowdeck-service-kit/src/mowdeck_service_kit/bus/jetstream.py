"""Event bus on NATS JetStream (nats-py).

On :meth:`JetStreamBus.connect` the streams from :data:`mowdeck_contracts.v1.subjects.STREAMS`
are created or updated (``manage_streams``), or awaited when another process manages them (the
``nats-streams`` job of an installation with per-service credentials, ADR 0073). Stream subjects
go through JetStream (persisted, de-duplicated by ``Nats-Msg-Id``, acknowledged); all other
subjects use core NATS. The client reconnects forever; connection state changes are logged and
reflected in :attr:`JetStreamBus.is_connected`, which backs the ``nats`` readiness check.

Least privilege (ADR 0073): the client's inboxes live below ``inbox_prefix`` (``_INBOX.<user>``
with credentials), and every stream consumer is created through the subject based API
(``$JS.API.CONSUMER.CREATE.<stream>.<name>.<filter>``) with its delivery subject in that inbox,
so the server's permissions can restrict both the filters a service may consume and the inboxes
it may read. A durable consumer created earlier with another delivery subject is moved to the
service's inbox when the service subscribes to it.
"""

import asyncio
import secrets
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
from mowdeck_service_kit.settings import NATS_INBOX, ServiceSettings

_log = get_logger(__name__)

_STREAM_POLL_SECONDS = 1.0


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


def durable_deliver_subject(inbox_prefix: str, stream: str, durable: str) -> str:
    """Delivery subject of a durable push consumer: ``<inbox_prefix>.durable.<stream>.<name>``.

    Stable, so that every replica of a service (a queue group on one durable) binds to the same
    subject, and inside the service's inbox, the only one its permissions let it read.
    """
    return f"{inbox_prefix}.durable.{stream}.{durable}"


@dataclass(eq=False)
class _NatsSubscriptionHandle:
    subscription: NatsSubscription | JetStreamContext.PushSubscription

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
        user: str | None = None,
        password: str | None = None,
        inbox_prefix: str | None = None,
        manage_streams: bool = True,
        stream_wait_seconds: float = 120.0,
    ) -> None:
        if (user is None) != (password is None):
            msg = "a NATS user needs a password and a password needs a user"
            raise ValueError(msg)
        self.servers = [servers] if isinstance(servers, str) else list(servers)
        self.name = name
        self.user = user
        self._password = password  # never logged
        self.inbox_prefix = inbox_prefix or (f"{NATS_INBOX}.{user}" if user else NATS_INBOX)
        self.manage_streams = manage_streams
        self.stream_wait_seconds = stream_wait_seconds
        self.streams = tuple(streams)
        self.max_deliver = max_deliver
        self.ack_wait_seconds = ack_wait_seconds
        self.nak_delay_seconds = nak_delay_seconds
        self.connect_timeout_seconds = connect_timeout_seconds
        self.publish_timeout_seconds = publish_timeout_seconds
        self.drain_timeout_seconds = drain_timeout_seconds
        self._nc: NatsClient | None = None
        self._js: JetStreamContext | None = None

    def __repr__(self) -> str:
        return f"JetStreamBus(servers={self.servers!r}, name={self.name!r}, user={self.user!r})"

    @classmethod
    def from_settings(cls, settings: ServiceSettings, **options: Any) -> "JetStreamBus":
        """Create a bus for ``settings.nats_url`` named after the service, with its credentials."""
        password = settings.nats_password.get_secret_value() if settings.nats_password else None
        defaults: dict[str, Any] = {
            "user": settings.nats_user,
            "password": password,
            "inbox_prefix": settings.nats_inbox_prefix,
            "manage_streams": settings.nats_streams_managed,
            "stream_wait_seconds": settings.nats_stream_wait_seconds,
        }
        return cls(settings.nats_servers, name=settings.service_name, **{**defaults, **options})

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

        credentials: dict[str, str] = {}
        if self.user is not None and self._password is not None:
            credentials = {"user": self.user, "password": self._password}
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
            inbox_prefix=self.inbox_prefix,
            **credentials,
        )
        self._js = self._nc.jetstream()
        await self.ensure_streams()
        _log.info("nats connected", servers=self.servers, user=self.user)

    async def ensure_streams(self) -> None:
        """Create missing streams and update existing ones to the contract settings.

        Without ``manage_streams`` the streams are only awaited: up to ``stream_wait_seconds``
        for each missing one, then :class:`BusError`.
        """
        js = self._jetstream()
        for spec in self.streams:
            config = stream_config(spec)
            try:
                await js.stream_info(spec.name)
            except nats.js.errors.NotFoundError:
                if not self.manage_streams:
                    await self._wait_for_stream(js, spec.name)
                    continue
                await js.add_stream(config)
                _log.info("stream created", stream=spec.name)
                continue
            if not self.manage_streams:
                continue
            try:
                await js.update_stream(config)
            except nats.js.errors.APIError as exc:
                _log.warning("stream update rejected", stream=spec.name, error=repr(exc))

    async def _wait_for_stream(self, js: JetStreamContext, name: str) -> None:
        _log.info("waiting for the stream (the nats-streams job creates it)", stream=name)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.stream_wait_seconds
        while loop.time() < deadline:
            await asyncio.sleep(_STREAM_POLL_SECONDS)
            try:
                await js.stream_info(name)
            except nats.js.errors.NotFoundError:
                continue
            return
        msg = (
            f"stream {name} does not exist after {self.stream_wait_seconds:g}s; the nats-streams "
            "job creates it (python -m mowdeck_service_kit.bus.streams, ADR 0073)"
        )
        raise BusError(msg)

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

    async def _consumer(
        self,
        stream: StreamSpec,
        subject: str,
        durable: str | None,
        queue: str | None,
        deliver_policy: DeliverPolicy,
    ) -> ConsumerConfig:
        """Create the push consumer (or move an existing durable one into this inbox)."""
        js = self._jetstream()
        config = ConsumerConfig(
            ack_policy=AckPolicy.EXPLICIT,
            ack_wait=self.ack_wait_seconds,
            max_deliver=self.max_deliver,
            deliver_policy=deliver_policy,
            deliver_group=queue,
            filter_subject=subject,
        )
        if durable is None:
            # An ephemeral consumer, named so that the subject based API (and with it the
            # filter in the API subject) applies; the server removes it once nobody listens.
            config.name = f"eph-{secrets.token_hex(10)}"
            config.deliver_subject = self._client().new_inbox()
            return (await js.add_consumer(stream.name, config)).config
        deliver = durable_deliver_subject(self.inbox_prefix, stream.name, durable)
        try:
            existing: ConsumerConfig = (await js.consumer_info(stream.name, durable)).config
        except nats.js.errors.NotFoundError:
            config.name = durable
            config.durable_name = durable
            config.deliver_subject = deliver
            return (await js.add_consumer(stream.name, config)).config
        if existing.deliver_subject == deliver:
            return existing
        # Created before ADR 0073 (a random _INBOX.<nuid>) or under another inbox prefix: keep
        # its position and settings, only deliver to this service's inbox from now on.
        # The answer of the update may still name the old delivery subject, so the binding uses
        # the configuration as sent.
        existing.name = existing.name or durable
        existing.deliver_subject = deliver
        await js.add_consumer(stream.name, existing)
        _log.info("durable consumer moved to the service inbox", stream=stream.name, name=durable)
        return existing

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
        if queue and durable and durable != queue:
            msg = f"a queue subscription uses the queue as durable name: {queue!r} != {durable!r}"
            raise ValueError(msg)
        durable = durable or queue

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

        deliver_policy = DeliverPolicy.LAST_PER_SUBJECT if last_per_subject else DeliverPolicy.NEW
        config = await self._consumer(stream, subject, durable, queue, deliver_policy)
        name = config.name or durable
        if name is None:  # pragma: no cover - the server always names a consumer
            msg = "the server returned a consumer without a name"
            raise BusError(msg)
        subscription = await self._jetstream().subscribe_bind(
            stream=stream.name, config=config, consumer=name, cb=callback, manual_ack=True
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
