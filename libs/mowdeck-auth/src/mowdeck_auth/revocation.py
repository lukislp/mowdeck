# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: Apache-2.0

"""Access that ended before its tokens did: the short-lived deny list (ADR 0082).

Access tokens are verified offline (:mod:`mowdeck_auth.verifier`), so a service accepts one until
it expires, whatever happened to its user meanwhile. That is fine for a sign-out. It is not fine
for a deletion: a service that just removed a deleted user's rows must not take a write from that
user's still valid token and store rows again.

Identity therefore publishes ``identity.access.revoked`` when a user is deleted (for the user and
for the API tokens that ended with them) and when the purge of an account starts (for the
account): "refuse every token of this subject, or of this account, that was issued up to now,
until they have all expired". Each process that verifies tokens keeps these in a
:class:`RevocationList` and its :class:`~mowdeck_auth.verifier.JwtVerifier` asks it after the
signature and the claims were checked.

* **One implementation.** The list belongs to the verifier; the kit attaches the resolver to the
  bus when the app starts (:meth:`JwtPrincipalResolver.attach
  <mowdeck_auth.resolver.JwtPrincipalResolver.attach>`), so a service has no code for it.
* **Every replica, and after a restart.** Each process has its own ephemeral consumer, and the
  subject carries the id (``event.identity.access.revoked.<id>``), so a process that starts
  later reads the newest entry of every id from the stream (``last_per_subject``).
* **Entries expire by themselves** at ``expires_at`` (the moment the last token issued before the
  revocation has run out, plus the allowed clock skew): the list stays small and nothing has to
  be removed from it.
* **It fails open, on purpose.** A process that cannot reach the bus keeps the entries it has
  and accepts tokens it never heard a revocation of, exactly as before this list existed, for
  at most the lifetime of an access token. Refusing every token while the bus is away would
  sign everybody out of every service, and with it take the emergency stop away. The account
  erasure does not rely on the list for this reason: Identity waits until every token of the
  account has expired before it asks a service to delete
  (``identity.domain.erasure.tokens_expired_at``).
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from mowdeck_contracts.v1 import AccessRevoked, AccessScope, EventEnvelope, Principal
from mowdeck_contracts.v1.subjects import ALL_ACCESS_REVOKED
from mowdeck_service_kit.bus import BusMessage, DropMessage, EventBus, Subscription
from pydantic import ValidationError

_log = logging.getLogger("mowdeck_auth.revocation")

MAX_ENTRIES = 10_000
"""Above this the list drops its oldest entries (they expire within minutes anyway). A bound, so
that nothing published on the bus can grow a process without limit."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


class RevocationList:
    """The revocations this process knows, by scope and id. Not thread safe; one event loop."""

    def __init__(
        self, clock: Callable[[], datetime] = _utc_now, max_entries: int = MAX_ENTRIES
    ) -> None:
        self._clock = clock
        self._max = max_entries
        self._entries: dict[tuple[AccessScope, UUID], AccessRevoked] = {}

    def __len__(self) -> int:
        self._drop_expired()
        return len(self._entries)

    def add(self, revoked: AccessRevoked) -> bool:
        """Remember a revocation; False when it has expired already (nothing to remember). A
        second one for the same id keeps the later of both limits."""
        now = self._clock()
        if revoked.expires_at <= now:
            return False
        key = (revoked.scope, revoked.id)
        known = self._entries.get(key)
        if known is not None:
            revoked = AccessRevoked(
                scope=revoked.scope,
                id=revoked.id,
                issued_before=max(known.issued_before, revoked.issued_before),
                expires_at=max(known.expires_at, revoked.expires_at),
            )
        self._entries[key] = revoked
        if len(self._entries) > self._max:
            self._drop_expired()
            for oldest in sorted(self._entries, key=lambda k: self._entries[k].expires_at)[
                : len(self._entries) - self._max
            ]:
                del self._entries[oldest]
        return True

    def refuses(self, principal: Principal, issued_at: datetime) -> bool:
        """Whether a token of ``principal`` issued at ``issued_at`` falls under a revocation:
        its subject (a user or an API token) or its account was revoked at or after the token
        was issued, and the revocation has not expired."""
        now = self._clock()
        keys = [(AccessScope.SUBJECT, principal.subject)]
        if principal.account_id is not None:
            keys.append((AccessScope.ACCOUNT, principal.account_id))
        for key in keys:
            entry = self._entries.get(key)
            if entry is None:
                continue
            if entry.expires_at <= now:
                del self._entries[key]
            elif issued_at <= entry.issued_before:
                return True
        return False

    def _drop_expired(self) -> None:
        now = self._clock()
        for key in [k for k, entry in self._entries.items() if entry.expires_at <= now]:
            del self._entries[key]


async def subscribe_revocations(bus: EventBus, revocations: RevocationList) -> Subscription:
    """Feed ``revocations`` from the bus: the newest stored entry of every id first (a process
    that starts while a revocation is still in force), then every new one.

    Ephemeral and unacknowledged: every replica needs every entry, and an entry that is lost is
    the fail-open case of the module documentation, not work that has to be repeated.
    """

    async def on_revoked(message: BusMessage) -> None:
        try:
            event = message.model(EventEnvelope[AccessRevoked])
        except ValidationError as exc:
            raise DropMessage(
                f"malformed access.revoked event: {exc.error_count()} errors"
            ) from exc
        if revocations.add(event.payload):
            _log.info(
                "access revoked",
                extra={"scope": event.payload.scope.value, "id": str(event.payload.id)},
            )

    return await bus.subscribe(
        ALL_ACCESS_REVOKED, on_revoked, last_per_subject=True, acknowledge=False
    )
