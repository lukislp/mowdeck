"""Apple Push Notification service (APNs) for the iOS app, token-based, without Firebase.

Requests go to ``POST https://<host>/3/device/<token>`` over HTTP/2 (APNs accepts nothing
else), so the channel owns an ``httpx.AsyncClient(http2=True)`` instead of the shared client;
tests inject one with a mock transport. The host follows ``MOWDECK_APNS_ENVIRONMENT``:
``api.sandbox.push.apple.com`` for development builds (installed from Xcode with a development
provisioning profile), ``api.push.apple.com`` for TestFlight and App Store builds.

Provider token: an ES256 JWT signed with the ``.p8`` key, header ``kid`` (key id), claims
``iss`` (team id) and ``iat``. Apple rejects tokens older than 60 minutes and throttles new ones
more often than every 20 minutes, so one token is reused for 50 minutes; a 403
``ExpiredProviderToken`` drops it at once (the delivery is retried with a fresh one).

Headers: ``apns-push-type: alert``, ``apns-priority`` 10 for every severity (visible alerts),
``apns-expiration`` now plus the Web Push TTL of the severity, ``apns-collapse-id`` the Web Push
``Topic`` of the tag (one visible notification per kind and mower). Body: ``aps`` with the
alert, the default sound, ``thread-id`` (the tag) and ``interruption-level``
(``time-sensitive`` for critical, else ``active``), plus ``url``, ``kind``, ``mower_id`` and
``notification_id`` for the app; at most 4096 bytes (a long body is shortened).

Answers: 200 sent; 410 (``Unregistered``) and 400 ``BadDeviceToken`` or
``DeviceTokenNotForTopic`` gone (the device is deleted); 403 ``ExpiredProviderToken`` retry with
a new token, any other 403 failed (wrong key, key id or team); 429 and 5xx retry (with
``Retry-After``); anything else failed. Apple's ``reason`` is part of the error.
"""

import json
import re
from datetime import timedelta
from typing import Any, Literal

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from mowdeck_contracts.v1 import NotificationSeverity

from notifications.adapters.channels import ChannelContext
from notifications.adapters.channels.webpush import TTL_SECONDS, retry_after, topic_for
from notifications.application.ports import Clock
from notifications.domain.models import Delivery, DeliveryOutcome, DeliveryResult

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
        timeout: float = 10.0,
    ) -> None:
        self._key = key
        self._key_id = key_id
        self._team_id = team_id
        self._bundle_id = bundle_id
        self._clock = clock
        self.host = HOSTS[environment]
        self._own_http = http is None
        self._http = http or httpx.AsyncClient(http2=True, follow_redirects=False)
        self._timeout = timeout
        self._token: tuple[str, float] | None = None

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
        expiration = self._clock().timestamp() + TTL_SECONDS[message.severity]
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
        try:
            response = await self._http.post(
                url, content=body, headers=headers, timeout=self._timeout
            )
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
    )
