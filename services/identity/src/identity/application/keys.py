"""Signing keys: creation, rotation and the published JWKS.

Statuses: ``next`` (published, not signing) -> ``active`` (signing) -> ``retiring`` (published
until its tokens expired) -> ``retired`` (gone). Rotation, run by the maintenance loop:

1. The active key is older than ``key_rotation`` and there is no next key: create a next key.
2. The next key is older than ``key_promote_after`` (default 1 h, well above the 5 minute JWKS
   cache of the services): promote it; the active key becomes retiring.
3. Retiring keys past ``retire_after`` (token lifetime plus skew, default 1 h) are retired.

So the JWKS shows at most the active key, one retiring key and the announced next key.
``rotate_now(immediate=True)`` (CLI ``rotate-keys --now``) activates a new key at once, for
example after a suspected leak; then also run ``retire --kid`` to drop the old key.
"""

import asyncio
import time
from datetime import datetime
from typing import Any

from jwt import PyJWK
from mowdeck_auth.jwks import parse_jwks
from mowdeck_auth.keys import SIGNING_ALGORITHM, SigningKey

from identity.application.context import IdentityConfig
from identity.application.ports import Clock, KeyCipher, Store, Tx
from identity.domain.errors import ConflictError, NotFoundError
from identity.domain.models import KeyStatus, SigningKeyRecord

PUBLISHED = (KeyStatus.NEXT, KeyStatus.ACTIVE, KeyStatus.RETIRING)


class KeyManager:
    """Holds the active signing key in memory and serves the JWKS (also a ``KeySource``)."""

    def __init__(
        self,
        store: Store,
        cipher: KeyCipher,
        clock: Clock,
        config: IdentityConfig,
        *,
        reload_cooldown: float = 5.0,
        monotonic: Any = time.monotonic,
    ) -> None:
        self.store = store
        self.cipher = cipher
        self.clock = clock
        self.config = config
        self._active: SigningKey | None = None
        self._jwks: dict[str, Any] = {"keys": []}
        self._public: dict[str, PyJWK] = {}
        self._reload_cooldown = reload_cooldown
        self._monotonic = monotonic
        self._loaded_at: float | None = None
        self._lock = asyncio.Lock()

    # --- serving -----------------------------------------------------------------------------

    def active_key(self) -> SigningKey:
        """The key that signs new tokens (``RuntimeError`` before :meth:`ensure`)."""
        if self._active is None:
            msg = "no active signing key loaded; call ensure() at startup"
            raise RuntimeError(msg)
        return self._active

    def jwks(self) -> dict[str, Any]:
        """The published key set."""
        return self._jwks

    async def key_for(self, kid: str) -> PyJWK | None:
        """``KeySource`` for Identity's own verifier; an unknown kid reloads (rate limited)."""
        key = self._public.get(kid)
        now = self._monotonic()
        if key is None and (
            self._loaded_at is None or now - self._loaded_at >= self._reload_cooldown
        ):
            await self.load()
            key = self._public.get(kid)
        return key

    # --- lifecycle -----------------------------------------------------------------------------

    async def ensure(self) -> None:
        """Create the first key if there is none, then load the keys.

        Two instances starting together may both try; the unique index on the active key lets
        one win, and the other simply loads the winner's key.
        """
        try:
            async with self.store.transaction() as tx:
                active = await tx.signing_keys.find(status=KeyStatus.ACTIVE.value)
                if not active:
                    await self._create(tx, KeyStatus.ACTIVE)
        except ConflictError:
            pass
        await self.load()

    async def load(self) -> None:
        """Load the published keys and decrypt the active one."""
        async with self._lock:
            async with self.store.transaction() as tx:
                records = await tx.signing_keys.find(order_by="created_at")
            published = [r for r in records if r.status in PUBLISHED]
            active = [r for r in published if r.status is KeyStatus.ACTIVE]
            if active:
                newest = max(active, key=lambda r: r.activated_at or r.created_at)
                self._active = SigningKey.from_raw(
                    self.cipher.decrypt(newest.kid, newest.private_key_enc)
                )
            self._jwks = {"keys": [dict(r.public_jwk) for r in published]}
            self._public = parse_jwks(self._jwks)
            self._loaded_at = self._monotonic()

    async def _create(self, tx: Tx, status: KeyStatus) -> SigningKeyRecord:
        key = SigningKey.generate()
        now = self.clock()
        record = SigningKeyRecord(
            kid=key.kid,
            alg=SIGNING_ALGORITHM,
            public_jwk=key.public_jwk(),
            private_key_enc=self.cipher.encrypt(key.kid, key.raw()),
            status=status,
            created_at=now,
            activated_at=now if status is KeyStatus.ACTIVE else None,
        )
        await tx.signing_keys.add(record)
        return record

    async def rotate_step(self) -> list[str]:
        """One maintenance step (see the module docstring); returns what it did."""
        done: list[str] = []
        now = self.clock()
        async with self.store.transaction() as tx:
            records = await tx.signing_keys.find()
            active = [r for r in records if r.status is KeyStatus.ACTIVE]
            nexts = [r for r in records if r.status is KeyStatus.NEXT]
            for record in records:
                if (
                    record.status is KeyStatus.RETIRING
                    and record.retire_after
                    and now >= record.retire_after
                ):
                    record.status = KeyStatus.RETIRED
                    await tx.signing_keys.update(record)
                    done.append(f"retired {record.kid}")
            if not active:
                created = await self._create(tx, KeyStatus.ACTIVE)
                done.append(f"created active {created.kid}")
            elif nexts:
                ready = [n for n in nexts if now - n.created_at >= self.config.key_promote_after]
                if ready:
                    await self._promote(tx, ready[0], active, now)
                    done.append(f"promoted {ready[0].kid}")
            else:
                newest = max(active, key=lambda r: r.activated_at or r.created_at)
                if now - (newest.activated_at or newest.created_at) >= self.config.key_rotation:
                    created = await self._create(tx, KeyStatus.NEXT)
                    done.append(f"created next {created.kid}")
        if done:
            await self.load()
        return done

    async def _promote(
        self, tx: Tx, record: SigningKeyRecord, active: list[SigningKeyRecord], now: datetime
    ) -> None:
        for old in active:
            old.status = KeyStatus.RETIRING
            old.retire_after = now + self.config.key_retire_after
            await tx.signing_keys.update(old)
        record.status = KeyStatus.ACTIVE
        record.activated_at = now
        await tx.signing_keys.update(record)

    async def rotate_now(self, *, immediate: bool) -> SigningKeyRecord:
        """Create a next key (or, with ``immediate``, a new active key right away)."""
        now = self.clock()
        async with self.store.transaction() as tx:
            records = await tx.signing_keys.find()
            if not immediate:
                existing = [r for r in records if r.status is KeyStatus.NEXT]
                record = existing[0] if existing else await self._create(tx, KeyStatus.NEXT)
            else:
                record = await self._create(tx, KeyStatus.NEXT)
                await self._promote(
                    tx, record, [r for r in records if r.status is KeyStatus.ACTIVE], now
                )
        await self.load()
        return record

    async def retire(self, kid: str) -> None:
        """Retire a key at once (emergency); tokens signed with it fail from now on."""
        async with self.store.transaction() as tx:
            record = await tx.signing_keys.get(kid=kid)
            if record is None:
                msg = f"no key {kid}"
                raise NotFoundError(msg)
            record.status = KeyStatus.RETIRED
            await tx.signing_keys.update(record)
            if not await tx.signing_keys.find(status=KeyStatus.ACTIVE.value):
                await self._create(tx, KeyStatus.ACTIVE)
        await self.load()

    async def list_keys(self) -> list[SigningKeyRecord]:
        """All keys, oldest first."""
        async with self.store.transaction() as tx:
            return await tx.signing_keys.find(order_by="created_at")
