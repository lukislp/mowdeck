"""Ed25519 signing keys and their public JWK form (RFC 8037).

The key id (``kid``) is the RFC 7638 thumbprint of the public key, so the same key always has
the same id.
"""

import base64
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

SIGNING_ALGORITHM = "EdDSA"
"""JOSE algorithm used for signing (RFC 8037). PyJWT 2.15.1 does not implement the fully
specified ``Ed25519`` name of RFC 9864, so ``EdDSA`` is the only accepted value for now."""

ALLOWED_ALGORITHMS: tuple[str, ...] = (SIGNING_ALGORITHM,)
"""Algorithms a verifier accepts (never ``none``, never HMAC)."""


def b64url(data: bytes) -> str:
    """Base64url without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    """Inverse of :func:`b64url` (padding optional)."""
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def thumbprint(public_key: Ed25519PublicKey) -> str:
    """RFC 7638 JWK thumbprint (SHA-256, base64url) of an Ed25519 public key."""
    canonical = json.dumps(
        {"crv": "Ed25519", "kty": "OKP", "x": b64url(public_key.public_bytes_raw())},
        separators=(",", ":"),
        sort_keys=True,
    )
    return b64url(hashlib.sha256(canonical.encode("ascii")).digest())


def public_jwk(public_key: Ed25519PublicKey, kid: str | None = None) -> dict[str, Any]:
    """The public JWK: ``{"kty": "OKP", "crv": "Ed25519", "x", "kid", "alg", "use"}``."""
    return {
        "kty": "OKP",
        "crv": "Ed25519",
        "x": b64url(public_key.public_bytes_raw()),
        "kid": kid or thumbprint(public_key),
        "alg": SIGNING_ALGORITHM,
        "use": "sig",
    }


@dataclass(frozen=True, slots=True)
class SigningKey:
    """A private signing key with its key id."""

    private_key: Ed25519PrivateKey = field(repr=False)
    kid: str

    @classmethod
    def generate(cls) -> "SigningKey":
        """A new random key; the kid is its thumbprint."""
        private_key = Ed25519PrivateKey.generate()
        return cls(private_key, thumbprint(private_key.public_key()))

    @classmethod
    def from_raw(cls, raw: bytes) -> "SigningKey":
        """Load from the 32 raw private bytes."""
        private_key = Ed25519PrivateKey.from_private_bytes(raw)
        return cls(private_key, thumbprint(private_key.public_key()))

    def raw(self) -> bytes:
        """The 32 raw private bytes (keep secret, encrypt at rest)."""
        return self.private_key.private_bytes_raw()

    @property
    def public_key(self) -> Ed25519PublicKey:
        """The public half."""
        return self.private_key.public_key()

    def public_jwk(self) -> dict[str, Any]:
        """The public JWK of this key."""
        return public_jwk(self.public_key, self.kid)
