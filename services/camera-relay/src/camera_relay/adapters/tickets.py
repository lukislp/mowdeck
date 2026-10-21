"""Ed25519 tickets: ``v1.<payload>.<signature>`` (base64url without padding).

The payload is compact JSON with the scope of :class:`~camera_relay.domain.tickets.
TicketClaims`: ``acc`` (account), ``mow`` (mower), ``rid`` (the session's random path token),
``lse`` (lease), ``pth`` (media paths), ``iat`` and ``exp`` (Unix seconds). The signature covers
``v1.<payload>``. The media server's auth callback (a later step) verifies a ticket with
:meth:`Ed25519TicketSigner.verify` and checks the requested path with
``TicketClaims.allows``.

The key comes from ``MOWDECK_RELAY_TICKET_SIGNING_KEY`` (or ``_FILE``): a PEM private key or the
base64 encoded 32-byte seed. Without one the relay creates a key per process, which is enough
for a single replica that also verifies its own tickets (tickets live 60 s).
"""

import base64
import binascii
import json
import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from camera_relay.domain.tickets import InvalidTicketError, TicketClaims

VERSION = "v1"
MAX_TICKET_LENGTH = 2048
_B64URL = re.compile(r"^[A-Za-z0-9_-]*$")


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    """Strict base64url without padding: one spelling per value, so a ticket cannot be
    altered (``urlsafe_b64decode`` alone skips foreign characters and ignores unused bits)."""
    if not _B64URL.match(text):
        msg = "not base64url"
        raise ValueError(msg)
    data = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    if _b64encode(data) != text:
        msg = "not canonical base64url"
        raise ValueError(msg)
    return data


def _epoch(at: datetime) -> int:
    return int(at.timestamp())


def encode_claims(claims: TicketClaims) -> bytes:
    """The canonical JSON payload of ``claims``."""
    payload = {
        "acc": str(claims.account_id),
        "mow": str(claims.mower_id),
        "rid": claims.relay_id,
        "lse": str(claims.lease_id),
        "pth": list(claims.paths),
        "iat": _epoch(claims.issued_at),
        "exp": _epoch(claims.expires_at),
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def decode_claims(data: bytes) -> TicketClaims:
    """The scope of a payload; raises ``InvalidTicketError`` when it is malformed."""
    try:
        payload: dict[str, Any] = json.loads(data)
        paths = payload["pth"]
        if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
            raise TypeError("pth")
        return TicketClaims(
            account_id=UUID(payload["acc"]),
            mower_id=UUID(payload["mow"]),
            relay_id=str(payload["rid"]),
            lease_id=UUID(payload["lse"]),
            paths=tuple(paths),
            issued_at=datetime.fromtimestamp(int(payload["iat"]), UTC),
            expires_at=datetime.fromtimestamp(int(payload["exp"]), UTC),
        )
    except (ValueError, KeyError, TypeError, OverflowError, OSError, RecursionError) as exc:
        # OverflowError and OSError: a time beyond what datetime holds; RecursionError: JSON
        # nested too deeply.
        msg = "malformed ticket payload"
        raise InvalidTicketError(msg) from exc


def load_private_key(value: str) -> Ed25519PrivateKey:
    """A PEM (PKCS#8) Ed25519 private key or the base64 encoded 32-byte seed."""
    text = value.strip()
    if text.startswith("-----BEGIN"):
        key = serialization.load_pem_private_key(text.encode("ascii"), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            msg = "the ticket signing key must be an Ed25519 key"
            raise ValueError(msg)
        return key
    try:
        seed = base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_", validate=False)
    except binascii.Error as exc:
        msg = "the ticket signing key is neither PEM nor base64"
        raise ValueError(msg) from exc
    if len(seed) != 32:
        msg = "the ticket signing key must be a 32-byte Ed25519 seed"
        raise ValueError(msg)
    return Ed25519PrivateKey.from_private_bytes(seed)


def verify_ticket(ticket: str, public_key: Ed25519PublicKey, now: datetime) -> TicketClaims:
    """The scope of a ticket signed by ``public_key``'s private key and valid at ``now``."""
    if len(ticket) > MAX_TICKET_LENGTH:
        msg = "ticket too long"
        raise InvalidTicketError(msg)
    parts = ticket.split(".")
    if len(parts) != 3 or parts[0] != VERSION:
        msg = "not a relay ticket"
        raise InvalidTicketError(msg)
    try:
        payload = _b64decode(parts[1])
        signature = _b64decode(parts[2])
    except (binascii.Error, ValueError) as exc:
        msg = "malformed ticket encoding"
        raise InvalidTicketError(msg) from exc
    try:
        public_key.verify(signature, f"{parts[0]}.{parts[1]}".encode("ascii"))
    except InvalidSignature as exc:
        msg = "bad ticket signature"
        raise InvalidTicketError(msg) from exc
    claims = decode_claims(payload)
    if now >= claims.expires_at:
        msg = "ticket expired"
        raise InvalidTicketError(msg)
    return claims


class Ed25519TicketSigner:
    """The ``TicketSigner`` port with an Ed25519 key."""

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self._private_key = private_key
        self.public_key = private_key.public_key()

    @classmethod
    def generate(cls) -> "Ed25519TicketSigner":
        """A signer with a new random key (valid for this process only)."""
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def from_setting(cls, value: str) -> "Ed25519TicketSigner":
        """A signer from ``MOWDECK_RELAY_TICKET_SIGNING_KEY`` (PEM or base64 seed)."""
        return cls(load_private_key(value))

    def sign(self, claims: TicketClaims) -> str:
        """The signed ticket of ``claims``."""
        signed = f"{VERSION}.{_b64encode(encode_claims(claims))}"
        signature = self._private_key.sign(signed.encode("ascii"))
        return f"{signed}.{_b64encode(signature)}"

    def verify(self, ticket: str, now: datetime) -> TicketClaims:
        """The scope of a valid ticket of this key; raises ``InvalidTicketError`` otherwise."""
        return verify_ticket(ticket, self.public_key, now)
