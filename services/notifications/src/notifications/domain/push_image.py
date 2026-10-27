"""Signed, short-lived URLs of the push notification image (ADR 0053).

The APNs payload of an error or theft notification carries one URL on the owner's own server;
the iOS Notification Service Extension downloads the photo from it. The photo never goes to
Apple. The URL holds nothing but the notification id (a random UUID, per user and event), an
expiry and an HMAC-SHA256 signature over both, keyed with a server secret:

    <public base>/api/v1/notifications/push-images/<notification id>?e=<unix seconds>&s=<signature>

No user, account, mower or device id and no token appears in it. Whoever holds the URL can fetch
that one photo until it expires (minutes), nothing else; the signature binds it to the
notification and the expiry, and it is checked in constant time. Pure functions, no I/O.
"""

import base64
import hashlib
import hmac
from datetime import datetime, timedelta
from uuid import UUID

PURPOSE = b"mowdeck:notifications:push-image:v1"
"""Mixed into every signature, so the key could be reused for nothing else by accident."""

PATH = "/api/v1/notifications/push-images"
DEFAULT_TTL = timedelta(minutes=10)
"""How long a push image URL stays valid after the push was sent."""


def derive_secret(material: str) -> bytes:
    """A 32 byte signing key from configured secret material (the APNs key, when no explicit
    ``MOWDECK_NOTIFICATIONS_IMAGE_SECRET`` is given)."""
    return hashlib.sha256(PURPOSE + b"|key|" + material.encode("utf-8")).digest()


def _signature(secret: bytes, notification_id: UUID, expires: int) -> str:
    message = PURPOSE + b"|" + str(notification_id).encode("ascii") + b"|" + str(expires).encode()
    digest = hmac.new(secret, message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class ImageLinks:
    """Issues and verifies the URLs."""

    def __init__(self, secret: bytes, base_url: str, ttl: timedelta = DEFAULT_TTL) -> None:
        if len(secret) < 16:
            msg = "the push image secret must be at least 16 bytes"
            raise ValueError(msg)
        self._secret = secret
        self._base = base_url.rstrip("/")
        self.ttl = ttl

    def url(self, notification_id: UUID, now: datetime) -> str:
        """The signed URL, valid for :attr:`ttl` from ``now``."""
        expires = int((now + self.ttl).timestamp())
        signature = _signature(self._secret, notification_id, expires)
        return f"{self._base}{PATH}/{notification_id}?e={expires}&s={signature}"

    def verify(self, notification_id: UUID, expires: int, signature: str, now: datetime) -> bool:
        """True for a genuine signature that has not expired."""
        expected = _signature(self._secret, notification_id, expires)
        return hmac.compare_digest(expected, signature) and now.timestamp() < expires

    def genuine(self, notification_id: UUID, expires: int, signature: str) -> bool:
        """True for a genuine signature, expired or not (to tell 410 from 404)."""
        return hmac.compare_digest(_signature(self._secret, notification_id, expires), signature)
