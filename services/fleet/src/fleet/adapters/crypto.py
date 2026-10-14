"""AES-256-GCM encryption of linked account passwords at rest (ADR 0034).

The scheme of Identity's signing keys (``identity.adapters.crypto``, ADR 0015): a 256-bit key
from ``MOWDECK_CREDENTIAL_ENCRYPTION_KEY`` (or ``_FILE``), base64 or hex; a random 96-bit nonce
per value; the linked account id as associated data, so a ciphertext copied to another row does
not decrypt. Stored form: ``version (1 byte) || key id (1 byte) || nonce (12) || ciphertext``;
the key id is the first byte of the key's SHA-256.

Rotation: put the old key into ``MOWDECK_CREDENTIAL_ENCRYPTION_KEY_PREVIOUS`` (comma separated
for several) and the new one into ``MOWDECK_CREDENTIAL_ENCRYPTION_KEY``. Values sealed with an
old key still decrypt; the next successful re-read of a linked account seals its password with
the new key (:meth:`AesGcmCipher.needs_reseal`).

Without a key :class:`DisabledCipher` answers ``enabled`` False and refuses to encrypt: Fleet
then offers no linked accounts (503).
"""

import base64
import binascii
import hashlib
import os
from collections.abc import Iterable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

FORMAT_VERSION = 1
DEV_KEY = b"dev-only-fleet-credential-key-00"
"""The marked development key of ``infra/compose/.env.example``; refused in production."""


def parse_key(value: str) -> bytes:
    """A 32 byte key from base64 (standard or url-safe) or 64 hex characters."""
    text = value.strip()
    try:
        if len(text) == 64:
            raw = bytes.fromhex(text)
        else:
            normalized = text.replace("-", "+").replace("_", "/")
            raw = base64.b64decode(normalized + "=" * (-len(normalized) % 4), validate=True)
    except (ValueError, binascii.Error) as exc:
        msg = "the credential encryption key must be base64 or hex"
        raise ValueError(msg) from exc
    if len(raw) != 32:
        msg = "the credential encryption key must be 32 bytes (256 bits)"
        raise ValueError(msg)
    return raw


def key_id(key: bytes) -> int:
    """The one-byte id of ``key`` in the stored form."""
    return hashlib.sha256(key).digest()[0]


class AesGcmCipher:
    """``SecretCipher`` port: AES-256-GCM with the context as associated data."""

    enabled = True

    def __init__(self, key: bytes, previous: Iterable[bytes] = ()) -> None:
        self._id = key_id(key)
        self._keys: list[tuple[int, AESGCM]] = [(self._id, AESGCM(key))]
        self._keys.extend((key_id(old), AESGCM(old)) for old in previous)

    def encrypt(self, context: str, value: str) -> bytes:
        """``version || key id || nonce || ciphertext`` with the current key."""
        nonce = os.urandom(12)
        sealed = self._keys[0][1].encrypt(nonce, value.encode("utf-8"), context.encode("utf-8"))
        return bytes([FORMAT_VERSION, self._id]) + nonce + sealed

    def decrypt(self, context: str, blob: bytes) -> str:
        """Inverse of :meth:`encrypt` with the current or a previous key; ``ValueError`` on an
        unknown key, a wrong context or tampering."""
        if len(blob) < 15 or blob[0] != FORMAT_VERSION:
            msg = "unknown credential format"
            raise ValueError(msg)
        for kid, aead in self._keys:
            if kid != blob[1]:
                continue
            try:
                plain = aead.decrypt(blob[2:14], blob[14:], context.encode("utf-8"))
            except InvalidTag:
                continue
            return plain.decode("utf-8")
        msg = "cannot decrypt the credential: wrong key or tampered data"
        raise ValueError(msg)

    def needs_reseal(self, blob: bytes) -> bool:
        """True when ``blob`` was sealed with a previous key."""
        return len(blob) < 2 or blob[1] != self._id


class DisabledCipher:
    """No key configured: nothing can be encrypted or decrypted."""

    enabled = False

    def encrypt(self, context: str, value: str) -> bytes:
        """Always refused."""
        msg = "MOWDECK_CREDENTIAL_ENCRYPTION_KEY is not set"
        raise ValueError(msg)

    def decrypt(self, context: str, blob: bytes) -> str:
        """Always refused."""
        msg = "MOWDECK_CREDENTIAL_ENCRYPTION_KEY is not set"
        raise ValueError(msg)

    def needs_reseal(self, blob: bytes) -> bool:
        """Nothing to reseal without a key."""
        return False


def cipher_from_key(
    value: str | None, previous: str | None = None
) -> AesGcmCipher | DisabledCipher:
    """The cipher for the configured key (``DisabledCipher`` when unset or empty);
    ``previous`` holds comma separated older keys that may still decrypt."""
    if value is None or not value.strip():
        return DisabledCipher()
    old = [parse_key(part) for part in (previous or "").split(",") if part.strip()]
    return AesGcmCipher(parse_key(value), old)
