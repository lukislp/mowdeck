"""Apple Push Notification service (APNs) for the iOS app, token-based, without Firebase.

Requests go to ``POST https://<host>/3/device/<token>`` over HTTP/2 (APNs accepts nothing
else), so the channel owns an ``httpx.AsyncClient(http2=True)`` instead of the shared client;
tests inject one with a mock transport (and a factory for the replacement client).

One request at a time: APNs first announces ``SETTINGS_MAX_CONCURRENT_STREAMS`` 200 and lowers
it to 1 right after (until the provider token is verified). httpcore 1.0.9 lowers its stream
limit by acquiring semaphore slots while it holds the connection's read lock, so with more than
one stream open the reader waits for slots that only readers can free: every request on the
connection hangs for good, and no HTTP timeout fires because nothing waits on the network
(MowDeck 0.9.1, 2026-10-23). With one stream at a time the slots are always free. The channel
therefore serialises its requests (``max_concurrency = 1`` for the outbox and a lock), and puts
each one under a deadline on top of the connect, read, write and pool timeouts. After a
deadline, a timeout or an HTTP/2 protocol error the client is replaced, so a broken connection
is never reused. The host follows ``MOWDECK_APNS_ENVIRONMENT``:
``api.sandbox.push.apple.com`` for development builds (installed from Xcode with a development
provisioning profile), ``api.push.apple.com`` for TestFlight and App Store builds.

Provider token: an ES256 JWT signed with the ``.p8`` key, header ``kid`` (key id), claims
``iss`` (team id) and ``iat``. Apple rejects tokens older than 60 minutes and throttles new ones
more often than every 20 minutes, so one token is reused for 50 minutes; a 403
``ExpiredProviderToken`` drops it at once (the delivery is retried with a fresh one).

Headers: ``apns-push-type: alert``, ``apns-priority`` 10 for every severity (visible alerts),
``apns-expiration`` the queue time plus the TTL of the severity, ``apns-collapse-id`` the Web Push
``Topic`` of the tag (one visible notification per kind and mower). Body: ``aps`` with the
alert, the default sound, ``thread-id`` (the tag) and ``interruption-level``
(``time-sensitive`` for critical, else ``active``), plus ``url``, ``kind``, ``mower_id`` and
``notification_id`` for the app; at most 4096 bytes (a long body is shortened).

Answers: 200 sent; 410 (``Unregistered``) and 400 ``BadDeviceToken`` or
``DeviceTokenNotForTopic`` gone (the device is deleted); 403 ``ExpiredProviderToken`` retry with
a new token, any other 403 failed (wrong key, key id or team); 429 and 5xx retry (with
``Retry-After``); anything else failed. Apple's ``reason`` is part of the error.
"""

import asyncio
import contextlib
import json
import re
from collections.abc import Callable
from datetime import timedelta
from functools import partial
from typing import Any, Literal

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from mowdeck_contracts.v1 import NotificationSeverity
from mowdeck_service_kit import get_logger

from notifications.adapters.channels import ChannelContext
from notifications.adapters.channels.webpush import retry_after, topic_for
from notifications.application.ports import Clock
from notifications.domain.models import TTL_SECONDS, Delivery, DeliveryOutcome, DeliveryResult

HOSTS: dict[str, str] = {
    "development": "api.sandbox.push.apple.com",
    "production": "api.push.apple.com",
}
# Every MowDeck notification is a visible alert, so all go out at priority 10 ("deliver now").
# Priority 5 lets iOS hold an alert for power reasons; on the owner's iPhone info alerts such as
# mowing_finished only appeared once the app was opened (2026-10-23). Severity still sets the
# interruption level and the expiration.
PRIORITY = {
    NotificationSeverity.CRITICAL: "10",
    NotificationSeverity.WARNING: "10",
    NotificationSeverity.INFO: "10",
}
MAX_PAYLOAD_BYTES = 4096
"""APNs limit of an alert notification's JSON body."""
TOKEN_LIFETIME = timedelta(minutes=50)
"""A provider token is reused this long (Apple: 20 to 60 minutes)."""
GONE_REASONS = frozenset({"BadDeviceToken", "DeviceTokenNotForTopic", "Unregistered"})
EXPIRED_TOKEN = "ExpiredProviderToken"  # noqa: S105 - an APNs error reason, not a secret

_HEX_TOKEN = re.compile(r"^[0-9a-fA-F]{64,200}$")

_log = get_logger("notifications.channel.apns")

KEEPALIVE_SECONDS = 300.0
"""An idle APNs connection is kept this long (Apple asks providers to keep connections open)."""


def default_client(timeout: float) -> httpx.AsyncClient:
    """The HTTP/2 client: explicit connect, read, write and pool timeouts, one connection."""
    return httpx.AsyncClient(
        http2=True,
        follow_redirects=False,
        timeout=httpx.Timeout(timeout),
        limits=httpx.Limits(
            max_connections=1, max_keepalive_connections=1, keepalive_expiry=KEEPALIVE_SECONDS
        ),
    )


def load_signing_key(pem: str) -> ec.EllipticCurvePrivateKey:
    """The P-256 private key of an ``AuthKey_<KEY_ID>.p8`` file (PEM, PKCS#8)."""
    try:
        key = load_pem_private_key(pem.strip().encode("utf-8"), password=None)
    except (ValueError, TypeError) as exc:
        msg = "MOWDECK_APNS_KEY is not a PEM private key (the contents of AuthKey_<KEY_ID>.p8)"
        raise ValueError(msg) from exc
    if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
        msg = "MOWDECK_APNS_KEY must be a P-256 (ES256) key"
        raise ValueError(msg)
    return key


def _reason(response: httpx.Response) -> str | None:
    try:
        data = response.json()
    except (ValueError, UnicodeDecodeError):
        return None
    reason = data.get("reason") if isinstance(data, dict) else None
    return str(reason) if reason else None


class ApnsChannel:
    """Sends to the iOS devices of a user."""

    name = "apns"
    max_concurrency = 1
    """The outbox sends one APNs request at a time (see the module docstring)."""

    def __init__(
        self,
        *,
        key: ec.EllipticCurvePrivateKey,
        key_id: str,
        team_id: str,
        bundle_id: str,
        clock: Clock,
        environment: Literal["development", "production"] = "development",
        http: httpx.AsyncClient | None = None,
        http_factory: Callable[[], httpx.AsyncClient] | None = None,
        timeout: float = 10.0,
        deadline: float | None = None,
    ) -> None:
        self._key = key
        self._key_id = key_id
        self._team_id = team_id
        self._bundle_id = bundle_id
        self._clock = clock
        self.host = HOSTS[environment]
        if http_factory is None and http is None:
            http_factory = partial(default_client, timeout)
        self._factory = http_factory
        self._own_http = http is None
        self._http = http if http is not None else self._new_client()
        self._timeout = httpx.Timeout(timeout)
        self._deadline = deadline if deadline is not None else 2 * timeout
        """Upper bound of one request, whatever httpx does (connect, write and read each have
        their own timeout)."""
        self._lock = asyncio.Lock()
        self._token: tuple[str, float] | None = None
        self.recycled = 0
        """How often the HTTP client was replaced (tests, diagnostics)."""

    def _new_client(self) -> httpx.AsyncClient:
        assert self._factory is not None  # noqa: S101 - only called with a factory
        return self._factory()

    async def _recycle(self, reason: str) -> None:
        """Replace the HTTP client after a timeout or protocol error (without a factory the
        injected client is kept)."""
        if self._factory is None:
            return
        old, own = self._http, self._own_http
        self._http, self._own_http = self._new_client(), True
        self.recycled += 1
        _log.warning("apns http client replaced", reason=reason)
        if own:
            # Closing a stuck connection must not hang the send either.
            with contextlib.suppress(Exception):
                async with asyncio.timeout(1):
                    await old.aclose()

    def provider_token(self) -> str:
        """The current provider token (a new one after :data:`TOKEN_LIFETIME`)."""
        now = self._clock().timestamp()
        if self._token is not None and now - self._token[1] < TOKEN_LIFETIME.total_seconds():
            return self._token[0]
        token = jwt.encode(
            {"iss": self._team_id, "iat": int(now)},
            self._key,
            algorithm="ES256",
            headers={"kid": self._key_id},
        )
        self._token = (token, now)
        return token

    def forget_token(self) -> None:
        """Drop the cached provider token (after ``ExpiredProviderToken``)."""
        self._token = None

    def payload(self, delivery: Delivery) -> bytes:
        """The JSON body (at most :data:`MAX_PAYLOAD_BYTES`)."""
        message = delivery.message
        critical = message.severity is NotificationSeverity.CRITICAL

        def encode(body: str) -> bytes:
            data: dict[str, Any] = {
                "aps": {
                    "alert": {"title": message.title, "body": body},
                    "sound": "default",
                    "thread-id": message.tag,
                    "interruption-level": "time-sensitive" if critical else "active",
                },
                "url": message.url,
                "kind": message.kind.value,
                "mower_id": str(message.mower_id) if message.mower_id else None,
                "notification_id": (
                    str(message.notification_id) if message.notification_id else None
                ),
            }
            compact = {k: v for k, v in data.items() if v is not None}
            return json.dumps(compact, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

        encoded = encode(message.body)
        if len(encoded) > MAX_PAYLOAD_BYTES:
            encoded = encode(message.body[:200])
        return encoded

    def request(self, delivery: Delivery) -> tuple[str, dict[str, str], bytes]:
        """URL, headers and body of one delivery."""
        token = str((delivery.target or {}).get("token", ""))
        if not _HEX_TOKEN.match(token):
            msg = "the device token is not hexadecimal"
            raise ValueError(msg)
        message = delivery.message
        queued = delivery.created_at or self._clock()
        expiration = queued.timestamp() + TTL_SECONDS[message.severity]
        headers = {
            "authorization": f"bearer {self.provider_token()}",
            "apns-topic": self._bundle_id,
            "apns-push-type": "alert",
            "apns-priority": PRIORITY[message.severity],
            "apns-expiration": str(int(expiration)),
            "apns-collapse-id": topic_for(message.tag),
            "content-type": "application/json",
        }
        return f"https://{self.host}/3/device/{token.lower()}", headers, self.payload(delivery)

    def classify(self, response: httpx.Response) -> DeliveryResult:
        """Map an APNs answer to a result."""
        status = response.status_code
        if 200 <= status < 300:
            return DeliveryResult.sent()
        reason = _reason(response)
        error = f"apns answered {status}" + (f": {reason}" if reason else "")
        if status == 410 or (status == 400 and reason in GONE_REASONS):
            return DeliveryResult(DeliveryOutcome.GONE, error)
        if status == 403 and reason == EXPIRED_TOKEN:
            self.forget_token()
            return DeliveryResult(DeliveryOutcome.RETRY, error)
        if status == 429 or status >= 500:
            return DeliveryResult(DeliveryOutcome.RETRY, error, retry_after(response))
        return DeliveryResult(DeliveryOutcome.FAILED, error)

    async def send(self, delivery: Delivery) -> DeliveryResult:
        """Sign (cached token) and post one notification."""
        try:
            url, headers, body = self.request(delivery)
        except ValueError as exc:
            return DeliveryResult(DeliveryOutcome.FAILED, f"invalid apns target: {exc}")
        async with self._lock:
            try:
                async with asyncio.timeout(self._deadline):
                    response = await self._http.post(
                        url, content=body, headers=headers, timeout=self._timeout
                    )
            except TimeoutError:
                await self._recycle("no answer within the request deadline")
                return DeliveryResult(
                    DeliveryOutcome.RETRY,
                    f"apns request got no answer within {self._deadline:g} s",
                )
            except (httpx.TimeoutException, httpx.ProtocolError) as exc:
                await self._recycle(type(exc).__name__)
                return DeliveryResult(DeliveryOutcome.RETRY, f"apns request failed: {exc!r}")
            except httpx.HTTPError as exc:
                return DeliveryResult(DeliveryOutcome.RETRY, f"apns request failed: {exc!r}")
        return self.classify(response)

    async def aclose(self) -> None:
        """Close the HTTP/2 client when the channel created it."""
        if self._own_http:
            await self._http.aclose()


def create_apns_channel(context: ChannelContext) -> ApnsChannel | None:
    """Entry point factory: None without the key, the key id or the team id."""
    settings = context.settings
    if settings.apns_key is None or not settings.apns_key_id or not settings.apns_team_id:
        return None
    return ApnsChannel(
        key=load_signing_key(settings.apns_key.get_secret_value()),
        key_id=settings.apns_key_id,
        team_id=settings.apns_team_id,
        bundle_id=settings.apns_bundle_id,
        clock=context.clock,
        environment=settings.apns_environment,
        timeout=settings.notifications_http_timeout_seconds,
        # Below the outbox deadline, so the channel (not the outbox) ends a stuck request and
        # replaces the client.
        deadline=min(
            2 * settings.notifications_http_timeout_seconds,
            0.9 * settings.notifications_send_deadline_seconds,
        ),
    )
