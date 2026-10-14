"""Argon2id password hashing and AES-GCM encryption of signing keys at rest.

Argon2id profiles (``MOWDECK_IDENTITY_ARGON2_PROFILE``):

| Profile | Memory | Iterations | Lanes | Source |
| --- | --- | --- | --- | --- |
| ``rfc9106_low`` (default) | 64 MiB | 3 | 4 | RFC 9106 second recommendation, argon2-cffi default |
| ``owasp_min`` | 19 MiB | 2 | 1 | OWASP Password Storage Cheat Sheet minimum |

Both meet or exceed the OWASP minimum. Hashing runs in worker threads, at most
``MOWDECK_IDENTITY_ARGON2_CONCURRENCY`` (default 4) at a time, which caps memory at
4 x 64 MiB = 256 MiB. Hashes made with other parameters are upgraded after the next login.
"""

import asyncio
import base64
import binascii
import os
import secrets

from argon2 import PasswordHasher as Argon2
from argon2 import Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ARGON2_PROFILES: dict[str, dict[str, int]] = {
    "rfc9106_low": {"time_cost": 3, "memory_cost": 65536, "parallelism": 4},
    "owasp_min": {"time_cost": 2, "memory_cost": 19456, "parallelism": 1},
    # Only for tests: fast, far below any recommendation. Never selectable by settings.
    "insecure_test": {"time_cost": 1, "memory_cost": 8, "parallelism": 1},
}


class Argon2PasswordHasher:
    """``PasswordHasher`` port with argon2-cffi (Argon2id)."""

    def __init__(self, profile: str = "rfc9106_low", concurrency: int = 4) -> None:
        params = ARGON2_PROFILES[profile]
        self._hasher = Argon2(
            time_cost=params["time_cost"],
            memory_cost=params["memory_cost"],
            parallelism=params["parallelism"],
            hash_len=32,
            salt_len=16,
            type=Type.ID,
        )
        self._semaphore = asyncio.Semaphore(max(1, concurrency))
        self._dummy: str | None = None

    async def hash(self, password: str) -> str:
        """Argon2id hash with a random salt."""
        async with self._semaphore:
            return await asyncio.to_thread(self._hasher.hash, password)

    async def verify(self, password_hash: str, password: str) -> bool:
        """Constant-time verification inside argon2; False on mismatch or a broken hash."""
        try:
            async with self._semaphore:
                return await asyncio.to_thread(self._hasher.verify, password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    def needs_rehash(self, password_hash: str) -> bool:
        """True if the parameters differ from the current profile."""
        try:
            return bool(self._hasher.check_needs_rehash(password_hash))
        except InvalidHashError:
            return True

    async def dummy_verify(self, password: str) -> None:
        """Verify against a fixed hash so that unknown users cost the same time."""
        if self._dummy is None:
            self._dummy = await self.hash(secrets.token_urlsafe(16))
        await self.verify(self._dummy, password)


def parse_kek(value: str) -> bytes:
    """A 32 byte key-encryption key from base64 (standard or url-safe) or 64 hex characters."""
    text = value.strip()
    try:
        if len(text) == 64:
            raw = bytes.fromhex(text)
        else:
            normalized = text.replace("-", "+").replace("_", "/")
            raw = base64.b64decode(normalized + "=" * (-len(normalized) % 4), validate=True)
    except (ValueError, binascii.Error) as exc:
        msg = "the key encryption key must be base64 or hex"
        raise ValueError(msg) from exc
    if len(raw) != 32:
        msg = "the key encryption key must be 32 bytes (256 bits)"
        raise ValueError(msg)
    return raw


class AesGcmKeyCipher:
    """``KeyCipher`` port: AES-256-GCM, a random 96-bit nonce per key, the kid as AAD."""

    def __init__(self, kek: bytes) -> None:
        self._aead = AESGCM(kek)

    def encrypt(self, kid: str, raw: bytes) -> bytes:
        """``nonce || ciphertext``."""
        nonce = os.urandom(12)
        return nonce + self._aead.encrypt(nonce, raw, kid.encode("utf-8"))

    def decrypt(self, kid: str, blob: bytes) -> bytes:
        """Inverse of :meth:`encrypt`; ``ValueError`` on a wrong key or tampering."""
        try:
            return self._aead.decrypt(blob[:12], blob[12:], kid.encode("utf-8"))
        except InvalidTag as exc:
            msg = "cannot decrypt the signing key: wrong key encryption key or tampered data"
            raise ValueError(msg) from exc
