# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Deleting an account for good (ADR 0082): the purge after the grace period.

``AccountService.request_deletion`` schedules a deletion (``deleting``, ``delete_after``); the
owner can restore it until then. This use case does the rest, in a background loop and on
demand:

1. **Begin.** An account whose grace period is over becomes ``purging``: all sessions end, the
   authorization tuples of its members and tokens go, and an :class:`AccountErasure` record
   names the services that must confirm. From here there is no way back. No token is issued for
   the account any more, and ``identity.access.revoked`` tells every service to refuse the ones
   that exist.
2. **Wait.** Access tokens are verified offline and live for minutes. No service is asked
   before the last one issued for the account has expired
   (:func:`~identity.domain.erasure.tokens_expired_at`, with the lifetime the issuer signs
   with): a service that confirmed must not get a write from a still valid token afterwards.
3. **Ask.** Every required service gets a core NATS request (``account.erase.<service>``, the
   port :class:`~identity.application.ports.AccountEraser`), in the order of the domain rule
   (:func:`~identity.domain.erasure.ask_order`: Fleet first, Billing before Entitlements),
   whatever order the installation listed them in. A confirmation is stored
   at once, so a retry skips it; a failure is stored with the reason and the next attempt waits
   longer each time (a service that is down is asked again, never skipped). A purge that runs
   longer than ``stall_after`` logs an error on every attempt: the account is locked and its
   data is not gone yet, which an operator has to look at (``python -m identity.cli erasures``).
4. **Finish.** When the last service confirmed, Identity removes the account's domain events
   from the message stream (:class:`~identity.application.event_erasure.EventErasureService`,
   a slice per attempt: the stream keeps them 30 days and only the envelope names the account),
   then deletes its own rows and
   tuples, scrubs the personal fields of the audit rows (the rows stay), writes
   ``account.erased`` and marks the record erased. An account that is gone from Identity is gone
   from every service and from the streams.

Nothing here talks to a service by name or to NATS: the port does.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from mowdeck_authz import api_token, user
from mowdeck_contracts.v1 import IDENTITY_ACCESS_REVOKED, AccessRevoked, AccessScope
from mowdeck_service_kit import get_logger

from identity.application.accounts import account_subjects
from identity.application.context import SYSTEM
from identity.application.deps import Deps
from identity.application.event_erasure import EventErasureService
from identity.application.ports import AccountEraser
from identity.application.sync import platform_tuple
from identity.application.users import delete_user_rows
from identity.domain.erasure import (
    AccountErasure,
    ErasureState,
    EventScope,
    grace_over,
    tokens_expired_at,
)
from identity.domain.models import AccountStatus

_log = get_logger("identity.erasure")

EVENT_STREAM = "event_stream"
"""Name of the step that removes the account's stored events, in the record's last error and in
the counts of the ``account.erased`` audit row."""


@dataclass(frozen=True, slots=True)
class ErasureConfig:
    """Which services must confirm and the timings of the purge (from the settings)."""

    services: tuple[str, ...]
    """The services that must confirm (:func:`~identity.domain.erasure.required_services`). The
    order they are asked in is the domain's, not this tuple's. Never empty in a real
    installation: Identity must not delete its rows while no service was asked."""
    settle: timedelta = timedelta(seconds=30)
    """Wait after the first service confirmed (Fleet removed the mowers, so their last states
    have landed before the other services delete)."""
    retry_max: timedelta = timedelta(minutes=10)
    """Longest wait between two attempts."""
    lease: timedelta = timedelta(minutes=5)
    """How long an attempt holds a purge; a second replica finds it not due meanwhile."""
    stall_after: timedelta = timedelta(hours=1)
    """A purge older than this that is still not finished is logged as an error."""


@dataclass(frozen=True, slots=True)
class ScheduledDeletion:
    """An account whose deletion is scheduled and whose purge has not begun."""

    account_id: UUID
    delete_after: datetime | None
    due: bool
    """The grace period is over: the next sweep starts the purge (where the purge is on). Until
    that sweep the owner can still restore the account."""


async def scheduled_deletions(deps: Deps) -> list[ScheduledDeletion]:
    """The accounts scheduled for deletion, the ones due first. For the operator (the start-up
    log line, ``python -m identity.cli erasures --scheduled``): it works with the purge switched
    off as well, which is when an operator wants to look before switching it on."""
    now = deps.clock()
    async with deps.store.transaction() as tx:
        deleting = await tx.accounts.find(status=AccountStatus.DELETING.value)
    found = [ScheduledDeletion(a.id, a.delete_after, grace_over(a, now)) for a in deleting]
    return sorted(found, key=lambda s: (not s.due, str(s.delete_after), str(s.account_id)))


@dataclass(frozen=True, slots=True)
class ErasureSweep:
    """What one sweep did."""

    begun: int = 0
    """Accounts whose grace period was over and whose purge started."""
    erased: int = 0
    """Accounts deleted completely."""
    pending: int = 0
    """Purges that continue later (a service failed, or the settle wait after Fleet)."""


class ErasureService:
    """Starts and carries on the purge of deleted accounts."""

    def __init__(
        self,
        deps: Deps,
        eraser: AccountEraser,
        config: ErasureConfig,
        events: EventErasureService,
    ) -> None:
        self.deps = deps
        self.eraser = eraser
        self.config = config
        self.events = events

    # --- loop ---------------------------------------------------------------------------------

    async def run(self, every_seconds: float = 60.0) -> None:
        """Sweep every ``every_seconds`` (and sooner when an attempt is due earlier)."""
        while True:
            try:
                await self.sweep()
                wait = await self.next_sweep_in(every_seconds)
            except Exception:
                _log.exception("the account erasure sweep failed")
                wait = every_seconds
            await asyncio.sleep(wait)

    async def next_sweep_in(self, every_seconds: float) -> float:
        """Seconds until the next sweep: ``every_seconds``, or less when an attempt is due
        earlier (at least a second)."""
        async with self.deps.store.transaction() as tx:
            purging = await tx.erasures.find(state=ErasureState.PURGING.value)
        due = [e.next_attempt_at for e in purging if e.next_attempt_at is not None]
        if not due:
            return every_seconds
        return min(every_seconds, max(1.0, (min(due) - self.deps.clock()).total_seconds()))

    async def sweep(self) -> ErasureSweep:
        """Start the purge of every account whose grace period is over, then carry on every
        purge that is due."""
        begun = await self.begin_due()
        results = [await self.advance(erasure) for erasure in await self._claim_due()]
        erased = sum(results)
        pending = len(results) - erased
        if begun or erased:
            _log.info("account erasure sweep", begun=begun, erased=erased, pending=pending)
        return ErasureSweep(begun, erased, pending)

    # --- begin --------------------------------------------------------------------------------

    async def begin_due(self) -> int:
        """Start the purge of the accounts whose grace period has passed; returns how many."""
        now = self.deps.clock()
        async with self.deps.store.transaction() as tx:
            deleting = await tx.accounts.find(status=AccountStatus.DELETING.value)
        due = [account.id for account in deleting if grace_over(account, now)]
        return sum([await self.begin(account_id) for account_id in due])

    async def begin(self, account_id: UUID) -> bool:
        """Start the purge of one account; False when it is no longer due (restored meanwhile,
        or its purge is already running). The owner's restore and this start decide in one
        transaction each, so exactly one wins."""
        if not self.config.services:
            _log.error("no service is configured for the account erasure; nothing is deleted")
            return False
        now = self.deps.clock()
        async with self.deps.uow() as work:
            tx = work.tx
            work.account_id = account_id
            account = await tx.accounts.get(id=account_id)
            if account is None or not grace_over(account, now):
                return False
            if await tx.erasures.get(account_id=account_id) is not None:
                return False
            subjects = await account_subjects(tx, account_id)
            members = await tx.memberships.find(account_id=account_id)
            account.status = AccountStatus.PURGING
            await tx.accounts.update(account)
            # The sessions end below and a purging account gets no new token; the ones issued
            # until now run out at ``expired``. The services are asked after that.
            expired = tokens_expired_at(now, self.deps.config.access_token_ttl)
            await tx.erasures.add(
                AccountErasure.begin(account_id, self.config.services, now, ask_from=expired)
            )
            work.emit(
                IDENTITY_ACCESS_REVOKED,
                account_id,
                AccessRevoked(
                    scope=AccessScope.ACCOUNT, id=account_id, issued_before=now, expires_at=expired
                ),
            )
            for member in members:
                await tx.sessions.update_where(
                    {"revoked_at": now, "revoke_reason": "account erasure"},
                    user_id=member.user_id,
                    revoked_at=None,
                )
            work.touch(*subjects)  # a purging account has no tuples for anybody
            await work.audit(
                SYSTEM,
                "account.erasure_started",
                account_id=account_id,
                target=("account", account_id),
                details={"services": list(self.config.services)},
            )
        _log.info("account erasure started", account_id=str(account_id))
        return True

    # --- ask and finish -----------------------------------------------------------------------

    async def _claim_due(self) -> list[AccountErasure]:
        """The purges whose next attempt is due, each held for ``lease`` so that another replica
        does not run the same one."""
        now = self.deps.clock()
        async with self.deps.store.transaction() as tx:
            due = [
                e for e in await tx.erasures.find(state=ErasureState.PURGING.value) if e.due(now)
            ]
            for erasure in due:
                erasure.lease(now, self.config.lease)
                await tx.erasures.update(erasure)
        return due

    async def advance(self, erasure: AccountErasure) -> bool:
        """Ask the services that did not confirm yet, then finish; True when the account is
        erased. Resumes where an earlier attempt stopped. A failure stores the reason, backs off
        and ends the attempt. Nothing is asked while an access token of the account can still be
        valid: the attempt is put off until then."""
        now = self.deps.clock()
        expired = tokens_expired_at(erasure.started_at, self.deps.config.access_token_ttl)
        if now < expired:
            erasure.lease(now, expired - now)
            await self._save(erasure)
            return False
        for service in erasure.open_services:
            try:
                deleted = await self.eraser.erase(service, erasure.account_id)
            except Exception as exc:
                await self._failed(erasure, service, exc)
                return False
            # After the first service (Fleet removed the mowers) the others wait, so the mowers'
            # last states have landed before they delete.
            settle = not erasure.confirmed and len(erasure.open_services) > 1
            wait = self.config.settle if settle else timedelta(0)
            erasure.confirm(service, deleted, self.deps.clock(), wait=wait)
            await self._save(erasure)
            if wait > timedelta(0):
                return False
        try:
            # Every service confirmed, so nothing publishes for the account any more: what the
            # event stream still holds of it goes now, before the rows that prove the purge.
            # One slice per attempt: a long stream neither outlives this attempt's lease nor
            # keeps the other purges of the sweep waiting. The position is in its own record.
            events = await self.events.advance(EventScope.ACCOUNT, erasure.account_id)
        except Exception as exc:
            await self._failed(erasure, EVENT_STREAM, exc)
            return False
        if events is None:
            erasure.lease(self.deps.clock(), timedelta(0))  # due again at once: the next slice
            await self._save(erasure)
            return False
        try:
            await self._erase_identity(erasure, events)
        except Exception as exc:
            await self._failed(erasure, "identity", exc)
            return False
        _log.info("account erased", account_id=str(erasure.account_id), counts=dict(erasure.counts))
        return True

    async def _save(self, erasure: AccountErasure) -> None:
        async with self.deps.store.transaction() as tx:
            await tx.erasures.update(erasure)

    async def _failed(self, erasure: AccountErasure, step: str, exc: Exception) -> None:
        now = self.deps.clock()
        erasure.fail(step, repr(exc), now, maximum=self.config.retry_max)
        await self._save(erasure)
        fields = {
            "account_id": str(erasure.account_id),
            "step": step,
            "attempts": erasure.attempts,
            "confirmed": list(erasure.confirmed),
            "error": repr(exc),
        }
        if erasure.stalled(now, self.config.stall_after):
            _log.error("account erasure is stuck: the account is locked but not deleted", **fields)
        else:
            _log.warning("account erasure step failed, retrying later", **fields)

    async def _erase_identity(self, erasure: AccountErasure, events: int) -> None:
        """Identity's own rows and tuples of the account, the audit scrub, then the account
        itself and the record's final state, in one transaction. ``events``: how many stored
        events of the account were erased from the stream, for the audit row."""
        account_id = erasure.account_id
        async with self.deps.uow() as work:
            tx = work.tx
            work.account_id = account_id
            members = await tx.memberships.find(account_id=account_id)
            tokens = await tx.api_tokens.find(account_id=account_id)
            user_ids = [m.user_id for m in members]
            for user_id in user_ids:
                found = await tx.users.get(id=user_id)
                if found is not None:
                    await delete_user_rows(tx, found)
            await tx.api_tokens.delete(account_id=account_id)
            await tx.assignments.delete(account_id=account_id)
            await tx.memberships.delete(account_id=account_id)
            await tx.roles.delete(account_id=account_id)
            scrubbed = await tx.scrub_audit(account_id)
            await tx.accounts.delete(id=account_id)
            now = self.deps.clock()
            erasure.finish(now)
            await tx.erasures.update(erasure)
            await work.audit(
                SYSTEM,
                "account.erased",
                target=("account", account_id),
                details={
                    "counts": {
                        **erasure.counts,
                        "identity_audit_scrubbed": scrubbed,
                        EVENT_STREAM: events,
                    },
                    "services": list(erasure.confirmed),
                },
            )
            work.touch(*(user(i) for i in user_ids), *(api_token(t.id) for t in tokens))
        # The platform tuple belongs to no subject; deleting a missing one is a no-op.
        await self.deps.sync.writer.write([], [platform_tuple(account_id)])

    # --- reading ------------------------------------------------------------------------------

    async def records(self, *, open_only: bool = False) -> list[AccountErasure]:
        """The purge records, oldest first (for the operator)."""
        async with self.deps.store.transaction() as tx:
            rows = await tx.erasures.find(order_by="started_at")
        return [e for e in rows if not open_only or e.state is ErasureState.PURGING]
