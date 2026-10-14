"""The delivery worker: sends queued deliveries with retries and exponential backoff.

Every replica runs one worker loop. A round claims a batch of due deliveries (the store uses
``FOR UPDATE SKIP LOCKED`` and a lease, so replicas never send the same delivery at the same
time), sends them with bounded concurrency outside any transaction and records each result:

* ``sent``: done;
* ``retry`` (or an exception): pending again after ``backoff_base * 2^(attempts-1)`` capped at
  ``backoff_max`` (or the server's ``Retry-After`` when that is longer), ``failed`` after
  ``max_attempts``;
* ``gone``: the target no longer exists; a Web Push subscription or an APNs device is deleted,
  the delivery is ``dropped``;
* ``failed``: permanent error, no retry.

A crash after sending but before recording means the message goes out again after the lease
(at least once).
"""

import asyncio
from datetime import timedelta
from typing import Protocol

from mowdeck_service_kit import get_logger

from notifications.application.context import NotificationsConfig
from notifications.application.ports import Channels, Clock, Store
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

    async def run_once(self) -> int:
        """Claim and send one batch; returns the number of claimed deliveries."""
        claimed = await self._store.claim_deliveries(
            self._clock(), self._config.lease, self._config.batch_size
        )
        if not claimed:
            return 0
        gate = asyncio.Semaphore(self._config.send_concurrency)

        async def attempt(delivery: Delivery) -> None:
            async with gate:
                await self._attempt(delivery)

        await asyncio.gather(*(attempt(d) for d in claimed))
        return len(claimed)

    async def prune(self) -> int:
        """Delete finished deliveries older than the retention."""
        return await self._store.prune_deliveries(self._clock() - self._config.delivery_retention)

    async def _attempt(self, delivery: Delivery) -> None:
        if delivery.target_kind is not TargetKind.NONE and delivery.target is None:
            await self._finish(delivery, DeliveryStatus.DROPPED, "the target no longer exists")
            return
        channel = self._channels.get(delivery.channel)
        if channel is None:
            await self._finish(delivery, DeliveryStatus.FAILED, "channel not configured")
            return
        try:
            result = await channel.send(delivery)
        except Exception as exc:
            result = DeliveryResult(DeliveryOutcome.RETRY, error=repr(exc))
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
