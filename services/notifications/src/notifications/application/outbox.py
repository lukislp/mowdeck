"""The delivery worker: sends queued deliveries with retries and exponential backoff.

Every replica runs one worker loop. A round claims due deliveries per channel, at most as many
as the channel has free send slots (``send_concurrency``, lower when the channel sets
``max_concurrency``; APNs sends one at a time). The store uses ``FOR UPDATE SKIP LOCKED`` and a
lease, so replicas never send the same delivery at the same time. Every claimed delivery is
sent in its own task outside any transaction, so a slow or hung channel or target never holds
up the others, and the loop claims again as soon as a slot frees up. Each result is recorded:

* ``sent``: done;
* ``retry`` (or an exception, or no result within ``send_deadline``, after which the send is
  cancelled): pending again after ``backoff_base * 2^(attempts-1)`` capped at ``backoff_max``
  (or the server's ``Retry-After`` when that is longer), ``failed`` after ``max_attempts``;
* ``gone``: the target no longer exists; a Web Push subscription or an APNs device is deleted,
  the delivery is ``dropped``;
* ``failed``: permanent error, no retry;
* a delivery still pending after the TTL of its severity is ``dropped`` without sending (a late
  "offline" or "finished" alert misleads more than it helps).

``send_deadline`` stays below the lease, so a claim never expires while its send still runs. A
crash or hang after claiming means the delivery is claimed again once the lease has passed, by
any replica or after a restart (at least once). :meth:`Outbox.stalled_for` reports a stall: no
delivery finished for ``stall_after`` while work was due.
"""

import asyncio
import contextlib
from datetime import datetime, timedelta
from typing import Protocol

from mowdeck_service_kit import get_logger

from notifications.application.context import NotificationsConfig
from notifications.application.ports import Channels, Clock, NotificationChannel, Store
from notifications.domain.models import (
    Delivery,
    DeliveryOutcome,
    DeliveryResult,
    DeliveryStatus,
    TargetKind,
    backoff_delay,
)

_log = get_logger("notifications.outbox")

_MAX_ERROR = 500


class DeliveryObserver(Protocol):
    """Receives every finished attempt (metrics)."""

    def delivered(self, channel: str, status: str) -> None:
        """One attempt of ``channel`` ended with ``status`` (sent, retry, failed, dropped)."""
        ...


class Outbox:
    """Sends what the fan-out queued."""

    def __init__(
        self,
        store: Store,
        channels: Channels,
        config: NotificationsConfig,
        clock: Clock,
        observer: DeliveryObserver | None = None,
    ) -> None:
        self._store = store
        self._channels = channels
        self._config = config
        self._clock = clock
        self._observer = observer
        self._in_flight: dict[str, set[asyncio.Task[None]]] = {}
        self._finished = asyncio.Event()
        self._progress_at = clock()
        """Last time the outbox had nothing to do or finished a delivery."""

    async def run_once(self) -> int:
        """Claim one round, send it and wait until every send has finished (tests, tools).

        Returns the number of claimed deliveries.
        """
        claimed = await self.dispatch()
        await self.drain()
        return claimed

    async def dispatch(self) -> int:
        """Claim what fits into the free slots of every channel and start sending.

        Returns the number of claimed deliveries; the sends go on in the background.
        """
        now = self._clock()
        names = self._channels.names()
        claimed = 0
        for name in names:
            free = self.limit(name) - len(self._in_flight.get(name, ()))
            if free <= 0:
                continue
            batch = await self._store.claim_deliveries(
                now, self._config.lease, min(free, self._config.batch_size), channel=name
            )
            for delivery in batch:
                self._start(delivery)
            claimed += len(batch)
        # Deliveries of channels this replica does not have fail at once (no network involved).
        orphans = await self._store.claim_deliveries(
            now, self._config.lease, self._config.batch_size, exclude=names
        )
        for delivery in orphans:
            self._start(delivery)
        claimed += len(orphans)
        if claimed == 0 and not self.in_flight():
            self._progress_at = now
        return claimed

    def limit(self, channel: str) -> int:
        """Deliveries of ``channel`` this worker sends at the same time."""
        wanted = getattr(self._channels.get(channel), "max_concurrency", None)
        if isinstance(wanted, int) and wanted > 0:
            return min(wanted, self._config.send_concurrency)
        return self._config.send_concurrency

    def in_flight(self) -> int:
        """Deliveries being sent right now."""
        return sum(len(tasks) for tasks in self._in_flight.values())

    async def wait(self, seconds: float) -> None:
        """Pause until a send finishes (a slot is free again) or ``seconds`` passed."""
        self._finished.clear()
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(seconds):
                await self._finished.wait()

    async def drain(self) -> None:
        """Wait until every running send has finished."""
        while tasks := [t for group in self._in_flight.values() for t in group]:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def aclose(self) -> None:
        """Cancel the running sends (shutdown); their claims expire after the lease."""
        tasks = [t for group in self._in_flight.values() for t in group]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def stalled_for(self, now: datetime | None = None) -> timedelta | None:
        """How long the outbox has made no progress, None while that is below ``stall_after``.

        Progress is a finished delivery or a round with nothing due and nothing in flight.
        """
        idle = (now or self._clock()) - self._progress_at
        return idle if idle > self._config.stall_after else None

    def _start(self, delivery: Delivery) -> None:
        task = asyncio.create_task(self._run(delivery), name=f"delivery-{delivery.id}")
        group = self._in_flight.setdefault(delivery.channel, set())
        group.add(task)

        def done(finished: asyncio.Task[None]) -> None:
            group.discard(finished)
            self._progress_at = self._clock()
            self._finished.set()

        task.add_done_callback(done)

    async def _run(self, delivery: Delivery) -> None:
        try:
            await self._attempt(delivery)
        except Exception:
            # Recording the result failed (database): the claim expires after the lease and
            # the delivery is sent again then.
            _log.exception(
                "delivery attempt failed, it is retried after the lease",
                delivery_id=str(delivery.id),
                channel=delivery.channel,
                attempts=delivery.attempts,
            )

    async def prune(self) -> int:
        """Delete finished deliveries older than the retention."""
        return await self._store.prune_deliveries(self._clock() - self._config.delivery_retention)

    async def _attempt(self, delivery: Delivery) -> None:
        expires_at = delivery.expires_at()
        if expires_at is not None and self._clock() >= expires_at:
            await self._finish(
                delivery,
                DeliveryStatus.DROPPED,
                f"expired before delivery ({delivery.message.severity.value} alerts are worth "
                f"sending until {expires_at:%Y-%m-%dT%H:%M:%SZ})",
            )
            return
        if delivery.target_kind is not TargetKind.NONE and delivery.target is None:
            await self._finish(delivery, DeliveryStatus.DROPPED, "the target no longer exists")
            return
        channel = self._channels.get(delivery.channel)
        if channel is None:
            await self._finish(delivery, DeliveryStatus.FAILED, "channel not configured")
            return
        result = await self._send(channel, delivery)
        match result.outcome:
            case DeliveryOutcome.SENT:
                await self._finish(delivery, DeliveryStatus.SENT, None)
            case DeliveryOutcome.GONE:
                if (
                    delivery.target_kind is TargetKind.PUSH_SUBSCRIPTION
                    and delivery.target_id is not None
                ):
                    await self._store.remove_push_subscription(delivery.target_id)
                    _log.info(
                        "push subscription removed, the push service reports it gone",
                        subscription_id=str(delivery.target_id),
                        user_id=str(delivery.user_id),
                    )
                elif delivery.target_kind is TargetKind.APNS_DEVICE and delivery.target_id:
                    await self._store.remove_apns_device(delivery.target_id)
                    _log.info(
                        "apns device removed, APNs reports the token unregistered",
                        device_id=str(delivery.target_id),
                        user_id=str(delivery.user_id),
                    )
                await self._finish(delivery, DeliveryStatus.DROPPED, result.error or "gone")
            case DeliveryOutcome.FAILED:
                await self._finish(delivery, DeliveryStatus.FAILED, result.error)
            case DeliveryOutcome.RETRY:
                await self._retry(delivery, result)

    async def _send(self, channel: NotificationChannel, delivery: Delivery) -> DeliveryResult:
        """``channel.send`` under the hard deadline; a hang or an exception means retry."""
        deadline = self._config.send_deadline.total_seconds()
        try:
            async with asyncio.timeout(deadline):
                return await channel.send(delivery)
        except TimeoutError:
            _log.error(
                "delivery send timed out, cancelled",
                delivery_id=str(delivery.id),
                channel=delivery.channel,
                attempts=delivery.attempts,
                deadline_seconds=deadline,
            )
            return DeliveryResult(
                DeliveryOutcome.RETRY, f"no result within the send deadline of {deadline:g} s"
            )
        except Exception as exc:
            _log.warning(
                "delivery send raised",
                delivery_id=str(delivery.id),
                channel=delivery.channel,
                attempts=delivery.attempts,
                error=repr(exc)[:_MAX_ERROR],
            )
            return DeliveryResult(DeliveryOutcome.RETRY, error=repr(exc))

    async def _retry(self, delivery: Delivery, result: DeliveryResult) -> None:
        error = result.error or "temporary failure"
        if delivery.attempts >= self._config.max_attempts:
            await self._finish(
                delivery,
                DeliveryStatus.FAILED,
                f"gave up after {delivery.attempts} attempts: {error}",
            )
            return
        delay = backoff_delay(
            delivery.attempts, self._config.backoff_base, self._config.backoff_max
        )
        if result.retry_after is not None:
            delay = max(delay, min(result.retry_after, timedelta(days=1)))
        now = self._clock()
        await self._store.finish_delivery(
            delivery.id,
            DeliveryStatus.PENDING,
            now=now,
            error=error[:_MAX_ERROR],
            next_attempt_at=now + delay,
        )
        self._observe(delivery.channel, "retry")
        _log.info(
            "delivery will be retried",
            delivery_id=str(delivery.id),
            channel=delivery.channel,
            attempts=delivery.attempts,
            retry_in_seconds=delay.total_seconds(),
            error=error[:_MAX_ERROR],
        )

    async def _finish(self, delivery: Delivery, status: DeliveryStatus, error: str | None) -> None:
        await self._store.finish_delivery(
            delivery.id, status, now=self._clock(), error=error[:_MAX_ERROR] if error else None
        )
        self._observe(delivery.channel, status.value)
        log = _log.info if status is DeliveryStatus.SENT else _log.warning
        log(
            "delivery finished",
            delivery_id=str(delivery.id),
            channel=delivery.channel,
            status=status.value,
            attempts=delivery.attempts,
            kind=delivery.message.kind.value,
            error=error,
        )

    def _observe(self, channel: str, status: str) -> None:
        if self._observer is not None:
            self._observer.delivered(channel, status)
