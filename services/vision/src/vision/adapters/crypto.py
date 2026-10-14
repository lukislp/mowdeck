"""AES-256-GCM for the users' provider secrets at rest (``MOWDECK_VISION_SECRETS_KEY``).

The key is 32 bytes given as base64 (standard or url-safe) or 64 hex characters, like
Identity's key-encryption key. Every value gets a random 96-bit nonce and the user and account
as associated data, so a ciphertext copied to another user's row does not decrypt.
"""

import base64
import binascii
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def parse_key(value: str) -> bytes:
    """A 32 byte key from base64 or hex; ``ValueError`` otherwise."""
    text = value.strip()
    try:
        if len(text) == 64:
            raw = bytes.fromhex(text)
        else:
            normalized = text.replace("-", "+").replace("_", "/")
            raw = base64.b64decode(normalized + "=" * (-len(normalized) % 4), validate=True)
    except (ValueError, binascii.Error) as exc:
        msg = "MOWDECK_VISION_SECRETS_KEY must be base64 or hex"
        raise ValueError(msg) from exc
    if len(raw) != 32:
        msg = "MOWDECK_VISION_SECRETS_KEY must be 32 bytes (256 bits)"
        raise ValueError(msg)
    return raw


class AesGcmSecretBox:
    """``SecretBox`` port: ``nonce || ciphertext`` with the associated data bound."""

    def __init__(self, key: bytes) -> None:
        self._aead = AESGCM(key)

    def encrypt(self, plaintext: bytes, aad: str) -> bytes:
        """Encrypt with a fresh nonce."""
        nonce = os.urandom(12)
        return nonce + self._aead.encrypt(nonce, plaintext, aad.encode("utf-8"))

    def decrypt(self, ciphertext: bytes, aad: str) -> bytes:
        """Inverse of :meth:`encrypt`; ``ValueError`` on a wrong key, data or binding."""
        try:
            return self._aead.decrypt(ciphertext[:12], ciphertext[12:], aad.encode("utf-8"))
        except (InvalidTag, ValueError) as exc:
            msg = "cannot decrypt: wrong secrets key or tampered data"
            raise ValueError(msg) from exc
