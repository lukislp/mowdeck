"""VAPID keys (RFC 8292) for Web Push, with ``py-vapid``.

Formats: the private key is the base64url encoded raw 32 byte P-256 scalar (what
``python -m notifications.cli generate-vapid-keys`` prints), a base64url DER key, or a PEM file
content; the public key is the base64url uncompressed point (65 bytes), the
``applicationServerKey`` a browser needs for ``pushManager.subscribe``.
"""

import base64
import os
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from py_vapid import Vapid02


def b64url(data: bytes) -> str:
    """Base64url without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


@dataclass(frozen=True, slots=True)
class VapidKeys:
    """A loaded VAPID key pair."""

    signer: Vapid02
    public_key: str
    """Base64url uncompressed public point (``applicationServerKey``)."""


def _public_of(key: ec.EllipticCurvePrivateKey) -> str:
    return b64url(
        key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
    )


def generate_private_key() -> str:
    """A new private key in the raw base64url form."""
    key = ec.generate_private_key(ec.SECP256R1())
    return b64url(key.private_numbers().private_value.to_bytes(32, "big"))


def load_keys(private_key: str, public_key: str | None = None) -> VapidKeys:
    """Load the pair; ``public_key``, when given, must belong to ``private_key``."""
    value = private_key.strip()
    if "-----BEGIN" in value:
        signer = Vapid02.from_pem(value.encode("ascii"))
    else:
        signer = Vapid02.from_string(value)
    derived = _public_of(signer.private_key)
    if public_key and public_key.strip() != derived:
        msg = "the VAPID public key does not belong to the private key"
        raise ValueError(msg)
    return VapidKeys(signer=signer, public_key=derived)


def load_or_create(path: Path) -> tuple[VapidKeys, bool]:
    """Load the private key from ``path`` or create it there (mode 0600, development only).

    Returns the keys and whether they were created.
    """
    if path.exists():
        return load_keys(path.read_text("ascii")), False
    path.parent.mkdir(parents=True, exist_ok=True)
    private = generate_private_key()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
        handle.write(private + "\n")
    return load_keys(private), True
