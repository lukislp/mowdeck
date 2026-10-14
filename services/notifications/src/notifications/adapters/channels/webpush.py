"""Web Push (RFC 8030) with VAPID (RFC 8292) and ``aes128gcm`` payload encryption (RFC 8291).

``pywebpush`` encrypts the payload for the subscription (``WebPusher.encode``) and ``py-vapid``
signs the VAPID JWT; the request itself goes out with the service's async ``httpx`` client, so
no thread or second HTTP stack is needed and tests can use a mock transport.

Headers: ``TTL`` and ``Urgency`` follow the severity (critical: 24 h and ``high``, warning: 6 h
and ``normal``, info: 1 h and ``low``); ``Topic`` (32 characters derived from the tag) lets the
push service replace an undelivered earlier message of the same kind and mower.

Answers: 2xx sent; 404 and 410 gone (the subscription is deleted); 429 and 5xx retry (with
``Retry-After``); anything else failed.
"""

import hashlib
import time
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx
from mowdeck_contracts.v1 import NotificationSeverity, WebPushPayload
from pywebpush import WebPusher, WebPushException

from notifications.adapters.channels import ChannelContext
from notifications.adapters.channels.vapid import VapidKeys
from notifications.domain.models import Delivery, DeliveryOutcome, DeliveryResult

TTL_SECONDS = {
    NotificationSeverity.CRITICAL: 24 * 3600,
    NotificationSeverity.WARNING: 6 * 3600,
    NotificationSeverity.INFO: 3600,
}
URGENCY = {
    NotificationSeverity.CRITICAL: "high",
    NotificationSeverity.WARNING: "normal",
    NotificationSeverity.INFO: "low",
}
MAX_PAYLOAD_BYTES = 3000
"""Plain payload limit; the encrypted record must stay below 4096 bytes."""


def retry_after(response: httpx.Response) -> timedelta | None:
    """The ``Retry-After`` header (seconds or an HTTP date) as a delay."""
    value = response.headers.get("retry-after")
    if not value:
        return None
    if value.strip().isdigit():
        return timedelta(seconds=int(value))
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(when - datetime.now(UTC), timedelta(0))


def classify(response: httpx.Response, channel: str) -> DeliveryResult:
    """Map a push service (or ntfy) answer to a result."""
    status = response.status_code
    if 200 <= status < 300:
        return DeliveryResult.sent()
    error = f"{channel} answered {status}"
    if status in (404, 410):
        return DeliveryResult(DeliveryOutcome.GONE, error)
    if status == 429 or status >= 500:
        return DeliveryResult(DeliveryOutcome.RETRY, error, retry_after(response))
    return DeliveryResult(DeliveryOutcome.FAILED, error)


def topic_for(tag: str) -> str:
    """A ``Topic`` header value: 32 URL-safe characters derived from ``tag``."""
    return hashlib.sha256(tag.encode("utf-8")).hexdigest()[:32]


class WebPushChannel:
    """Sends to the browser subscriptions of a user."""

    name = "webpush"

    def __init__(
        self, http: httpx.AsyncClient, vapid: VapidKeys, subject: str, timeout: float = 10.0
    ) -> None:
        self._http = http
        self._vapid = vapid
        self._subject = subject
        self._timeout = timeout

    def payload(self, delivery: Delivery) -> bytes:
        """The JSON the service worker receives."""
        message = delivery.message
        payload = WebPushPayload(
            title=message.title,
            body=message.body,
            url=message.url,
            tag=message.tag,
            kind=message.kind,
            mower_id=message.mower_id,
            notification_id=message.notification_id,
        )
        data = payload.model_dump_json(exclude_none=True).encode("utf-8")
        if len(data) > MAX_PAYLOAD_BYTES:
            data = (
                payload.model_copy(update={"body": payload.body[:200]})
                .model_dump_json(exclude_none=True)
                .encode("utf-8")
            )
        return data

    def request(self, delivery: Delivery) -> tuple[str, dict[str, str], bytes]:
        """Endpoint, headers and encrypted body of one delivery."""
        target = delivery.target or {}
        endpoint = str(target.get("endpoint", ""))
        parts = urlsplit(endpoint)
        if parts.scheme != "https" or not parts.netloc:
            msg = "the push endpoint must be an https URL"
            raise ValueError(msg)
        body = WebPusher(target).encode(self.payload(delivery), "aes128gcm")["body"]
        claims: dict[str, str | int] = {
            "sub": self._subject,
            "aud": f"{parts.scheme}://{parts.netloc}",
            "exp": int(time.time()) + 12 * 3600,
        }
        headers = dict(self._vapid.signer.sign(claims))
        severity = delivery.message.severity
        headers.update(
            {
                "TTL": str(TTL_SECONDS[severity]),
                "Urgency": URGENCY[severity],
                "Topic": topic_for(delivery.message.tag),
                "Content-Encoding": "aes128gcm",
                "Content-Type": "application/octet-stream",
            }
        )
        return endpoint, headers, bytes(body)

    async def send(self, delivery: Delivery) -> DeliveryResult:
        """Encrypt, sign and post one message."""
        try:
            endpoint, headers, body = self.request(delivery)
        except (WebPushException, ValueError, KeyError, TypeError) as exc:
            return DeliveryResult(DeliveryOutcome.FAILED, f"invalid subscription: {exc}")
        try:
            response = await self._http.post(
                endpoint, content=body, headers=headers, timeout=self._timeout
            )
        except httpx.HTTPError as exc:
            return DeliveryResult(DeliveryOutcome.RETRY, f"webpush request failed: {exc!r}")
        return classify(response, self.name)


def create_webpush_channel(context: ChannelContext) -> WebPushChannel | None:
    """Entry point factory: None without VAPID keys or subject."""
    subject = context.settings.notifications_vapid_subject
    if context.vapid is None or not subject:
        return None
    return WebPushChannel(
        context.http,
        context.vapid,
        subject,
        timeout=context.settings.notifications_http_timeout_seconds,
    )
