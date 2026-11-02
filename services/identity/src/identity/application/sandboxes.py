"""Demo sandboxes: one account per visitor of the public demo (ADR 0076).

"Try the demo" gives every visitor an own account with its own simulated mowers, schedules,
zones and inbox, so nobody sees another visitor's actions. The lifecycle of one sandbox:

1. **Pool.** A background worker keeps ``pool_size`` sandboxes ready: the account and its user
   (no password, nobody signs in with one), the setup tour marked done, then the provisioner
   fills the other services (mowers, schedules, notification preferences, a week of history).
   Filling takes seconds (the history); a click must not wait for it.
2. **Claim.** "Try the demo" takes the oldest ready sandbox and starts a login session of its
   user, the same tokens and cookie as any sign-in (``POST /demo/session``). With an empty pool
   a sandbox is made on the spot without history (about a second). The number of active
   sandboxes is capped (``max_active``): one more visitor gets :class:`DemoFull` with a time to
   come back; one address may start ``per_address_per_hour`` sandboxes an hour.
3. **Idle and expiry.** The sweep runs every minute. A sandbox whose user's newest login session
   row (every refresh writes one; the web app refreshes every few minutes while a tab is open)
   is older than ``idle``, whose login session ended (logout), or which is older than
   ``max_age`` goes to ``purging``. Its sessions are revoked at once.
4. **Purge.** Every service deletes the account's data through its demo purge endpoint (Fleet
   first, which also stops the simulated mowers; the others after ``settle`` so the mowers'
   last states have landed), then Identity deletes its own rows and the OpenFGA tuples, then
   the row. A failing service is retried with backoff; the services that confirmed are not
   asked again.

The periodic full reset of the demo stack stays the safety net behind all of this.
"""

import asyncio
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from mowdeck_authz import user
from mowdeck_service_kit import get_logger
from mowdeck_service_kit.client_address import limiter_key

from identity.application.accounts import AccountService, Registration
from identity.application.context import SYSTEM, RequestContext
from identity.application.deps import Deps
from identity.application.ports import Gt, Lt, SandboxProvisioner
from identity.application.ratelimit import TokenBucketLimiter
from identity.application.sessions import SessionService, TokenPair
from identity.application.sync import platform_tuple
from identity.domain.errors import DemoFull, RateLimited
from identity.domain.models import (
    DemoSandbox,
    OnboardingProgress,
    OnboardingStatus,
    SandboxState,
)

_log = get_logger("identity.sandboxes")

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_IDENTITY = "identity"
"""Name of Identity's own step in ``DemoSandbox.purged``."""


@dataclass(frozen=True, slots=True)
class SandboxConfig:
    """Limits and timings of the demo sandboxes (from the settings)."""

    max_active: int = 40
    """Sandboxes visitors hold at once; one more visitor gets "the demo is full"."""
    pool_size: int = 2
    """Ready sandboxes kept for the next visitors."""
    idle: timedelta = timedelta(minutes=30)
    """A sandbox without a session refresh for this long is deleted."""
    max_age: timedelta = timedelta(hours=6)
    """A sandbox is deleted this long after it was claimed, however active."""
    per_address_per_hour: int = 6
    """Sandboxes one client address (IPv6: its /64) may start an hour."""
    retry_after_seconds: int = 120
    """What a visitor of a full demo is told to wait."""
    provision_timeout: timedelta = timedelta(minutes=15)
    """A sandbox still being filled after this long is given up and deleted."""
    settle: timedelta = timedelta(seconds=30)
    """Wait between Fleet's purge (the mowers stop) and the other services' purges."""
    retry_max: timedelta = timedelta(minutes=10)
    """Longest wait between two purge attempts of one sandbox."""
    account_name: str = "MowDeck demo"
    display_name: str = "Demo visitor"


@dataclass(frozen=True, slots=True)
class SweepResult:
    """What one sweep did."""

    marked: int = 0
    """Sandboxes sent to the purge."""
    purged: int = 0
    """Sandboxes deleted completely."""
    pending: int = 0
    """Purges that continue later (a service failed, or the settle wait after Fleet)."""


class SandboxService:
    """Pool, claim, sweep and purge of the demo sandboxes."""

    def __init__(
        self,
        deps: Deps,
        accounts: AccountService,
        sessions: SessionService,
        provisioner: SandboxProvisioner,
        config: SandboxConfig,
    ) -> None:
        self.deps = deps
        self.accounts = accounts
        self.sessions = sessions
        self.provisioner = provisioner
        self.config = config
        per_hour = config.per_address_per_hour
        self._limiter = TokenBucketLimiter(per_minute=per_hour / 60.0, capacity=max(1, per_hour))
        self._claim = asyncio.Lock()
        self._fill = asyncio.Lock()
        self.wanted = asyncio.Event()
        """Set when the pool should be refilled (a claim took a sandbox)."""

    # --- claim --------------------------------------------------------------------------------

    async def enter(self, ctx: RequestContext) -> TokenPair:
        """ "Try the demo": a login session in a sandbox of the visitor's own.

        Raises :class:`RateLimited` (the address started too many) and :class:`DemoFull`.
        """
        wait = self._limiter.take(limiter_key(ctx.ip))
        if wait > 0:
            msg = "this address started many demo sandboxes; try again later"
            raise RateLimited(msg, int(wait) + 1)
        sandbox, fresh = await self._claim_one()
        self.wanted.set()
        if fresh:
            try:
                await self._fill_services(sandbox, history=False)
            except Exception as exc:
                _log.warning("a sandbox could not be prepared", error=repr(exc))
                await self._mark(sandbox, "provisioning failed")
                msg = "the demo could not prepare your sandbox; try again in a minute"
                raise DemoFull(msg, 60) from exc
            await self._set_state(sandbox, SandboxState.ACTIVE)
        _log.info("sandbox claimed", account_id=str(sandbox.account_id), fresh=fresh)
        return await self.sessions.login_sandbox(sandbox.user_id, sandbox.account_id, ctx)

    async def _claim_one(self) -> tuple[DemoSandbox, bool]:
        """A ready sandbox turned active, or a new reserved one (``True``) when the pool is
        empty. Serialised, so the cap holds under concurrent clicks."""
        async with self._claim:
            now = self.deps.clock()
            async with self.deps.store.transaction() as tx:
                taken = await tx.sandboxes.count(state=SandboxState.ACTIVE)
                taken += await tx.sandboxes.count(
                    state=SandboxState.PROVISIONING, claimed_at=Gt(_EPOCH)
                )
                if taken >= self.config.max_active:
                    msg = "every demo sandbox is taken; try again in a few minutes"
                    raise DemoFull(msg, self.config.retry_after_seconds)
                ready = await tx.sandboxes.find(
                    state=SandboxState.READY, order_by="created_at", limit=1
                )
                if ready:
                    sandbox = ready[0]
                    sandbox.state, sandbox.claimed_at = SandboxState.ACTIVE, now
                    await tx.sandboxes.update(sandbox)
                    return sandbox, False
            return await self._create(claimed_at=now), True

    # --- pool ---------------------------------------------------------------------------------

    async def fill_pool(self) -> int:
        """Make sandboxes until ``pool_size`` are ready or being filled; returns how many were
        made. One at a time (the history costs CPU); a failure ends this round."""
        made = 0
        async with self._fill:
            while True:
                async with self.deps.store.transaction() as tx:
                    pooled = await tx.sandboxes.count(state=SandboxState.READY)
                    pooled += await tx.sandboxes.count(
                        state=SandboxState.PROVISIONING, claimed_at=None
                    )
                if pooled >= self.config.pool_size:
                    return made
                sandbox = await self._create(claimed_at=None)
                try:
                    await self._fill_services(sandbox, history=True)
                except Exception as exc:
                    _log.warning("a pool sandbox could not be prepared", error=repr(exc))
                    await self._mark(sandbox, "provisioning failed")
                    return made
                await self._set_state(sandbox, SandboxState.READY)
                made += 1

    async def run_pool(self, every_seconds: float = 60.0) -> None:
        """Refill the pool when a claim asks for it, and every ``every_seconds``; at start only
        once the other services are ready (Identity usually starts first)."""
        while not await self._ready():  # noqa: ASYNC110 - polls other services
            await asyncio.sleep(5.0)
        while True:
            try:
                await self.fill_pool()
            except Exception:
                _log.exception("filling the sandbox pool failed")
            self.wanted.clear()
            try:
                async with asyncio.timeout(every_seconds):
                    await self.wanted.wait()
            except TimeoutError:
                pass

    async def _ready(self) -> bool:
        try:
            return await self.provisioner.ready()
        except Exception:
            return False

    async def _create(self, *, claimed_at: datetime | None) -> DemoSandbox:
        """The account, its password-less user, the setup tour marked done, and the row."""
        name = f"visitor-{secrets.token_hex(6)}"
        registered = await self.accounts.create(
            Registration(
                username=name,
                password="",
                account_name=self.config.account_name,
                display_name=self.config.display_name,
            ),
            None,
            SYSTEM,
            ignore_mode=True,
        )
        now = self.deps.clock()
        sandbox = DemoSandbox(
            account_id=registered.account_id,
            user_id=registered.user_id,
            state=SandboxState.PROVISIONING,
            created_at=now,
            claimed_at=claimed_at,
        )
        async with self.deps.store.transaction() as tx:
            await tx.sandboxes.add(sandbox)
            # Visitors land on the mowers, not in the setup tour of a new account (ADR 0062).
            await tx.onboarding.add(
                OnboardingProgress(
                    user_id=registered.user_id,
                    status=OnboardingStatus.COMPLETED,
                    skipped_steps=[],
                    dismissed_hints=[],
                    created_at=now,
                    updated_at=now,
                )
            )
        return sandbox

    async def _fill_services(self, sandbox: DemoSandbox, *, history: bool) -> None:
        token = await self.sessions.provisioning_token(sandbox.user_id, sandbox.account_id)
        await self.provisioner.provision(sandbox.account_id, token, history=history)

    # --- sweep and purge ----------------------------------------------------------------------

    async def sweep(self) -> SweepResult:
        """Mark idle, ended, expired and stuck sandboxes for deletion, then purge what is due."""
        now = self.deps.clock()
        marked = 0
        async with self.deps.store.transaction() as tx:
            active = await tx.sandboxes.find(state=SandboxState.ACTIVE)
            stuck = await tx.sandboxes.find(
                state=SandboxState.PROVISIONING, created_at=Lt(now - self.config.provision_timeout)
            )
        for sandbox in active:
            reason = await self._end_reason(sandbox, now)
            if reason is not None:
                await self._mark(sandbox, reason)
                marked += 1
        for sandbox in stuck:
            await self._mark(sandbox, "stuck")
            marked += 1
        results = await self.purge_due()
        purged = sum(results)
        pending = len(results) - purged
        if marked or purged:
            _log.info("sandbox sweep", marked=marked, purged=purged, pending=pending)
        return SweepResult(marked, purged, pending)

    async def _end_reason(self, sandbox: DemoSandbox, now: datetime) -> str | None:
        claimed = sandbox.claimed_at or sandbox.created_at
        if now - claimed >= self.config.max_age:
            return "expired"
        async with self.deps.store.transaction() as tx:
            sessions = await tx.sessions.find(user_id=sandbox.user_id)
        if sessions and all(s.revoked_at is not None for s in sessions):
            return "ended"
        last = max((s.created_at for s in sessions), default=claimed)
        if now - last >= self.config.idle:
            return "idle"
        return None

    async def _mark(self, sandbox: DemoSandbox, reason: str) -> None:
        """Send a sandbox to the purge and end its login sessions at once."""
        now = self.deps.clock()
        async with self.deps.store.transaction() as tx:
            current = await tx.sandboxes.get(account_id=sandbox.account_id)
            if current is None or current.state is SandboxState.PURGING:
                return
            current.state, current.purge_reason = SandboxState.PURGING, reason
            current.next_attempt_at = now
            await tx.sandboxes.update(current)
            for session in await tx.sessions.find(user_id=sandbox.user_id, revoked_at=None):
                session.revoked_at, session.revoke_reason = now, f"demo sandbox {reason}"
                await tx.sessions.update(session)
        _log.info("sandbox to be deleted", account_id=str(sandbox.account_id), reason=reason)

    async def _set_state(self, sandbox: DemoSandbox, state: SandboxState) -> None:
        async with self.deps.store.transaction() as tx:
            current = await tx.sandboxes.get(account_id=sandbox.account_id)
            if current is None or current.state is not SandboxState.PROVISIONING:
                return  # the sweep gave it up meanwhile
            current.state = state
            await tx.sandboxes.update(current)
        sandbox.state = state

    async def purge_due(self) -> list[bool]:
        """Purge every sandbox whose next attempt is due; one answer per sandbox."""
        now = self.deps.clock()
        async with self.deps.store.transaction() as tx:
            due = [
                s
                for s in await tx.sandboxes.find(state=SandboxState.PURGING)
                if s.next_attempt_at is None or s.next_attempt_at <= now
            ]
        return [await self.purge(sandbox) for sandbox in due]

    async def purge(self, sandbox: DemoSandbox) -> bool:
        """Delete the sandbox everywhere; True when it is gone. Resumes where an earlier
        attempt stopped; a failure schedules the next attempt with backoff."""
        services = self.provisioner.services
        for index, service in enumerate(services):
            if service in sandbox.purged:
                continue
            try:
                await self.provisioner.purge(service, sandbox.account_id)
            except Exception as exc:
                await self._failed(sandbox, service, exc)
                return False
            sandbox.purged.append(service)
            settle = index == 0 and len(services) > 1 and self.config.settle.total_seconds() > 0
            await self._progress(sandbox, settle=settle)
            if settle:
                return False  # the others after the mowers' last states have landed
        try:
            await self._purge_identity(sandbox)
        except Exception as exc:
            await self._failed(sandbox, _IDENTITY, exc)
            return False
        _log.info(
            "sandbox deleted", account_id=str(sandbox.account_id), reason=sandbox.purge_reason
        )
        return True

    async def _progress(self, sandbox: DemoSandbox, *, settle: bool) -> None:
        now = self.deps.clock()
        async with self.deps.store.transaction() as tx:
            current = await tx.sandboxes.get(account_id=sandbox.account_id)
            if current is None:
                return
            current.purged = list(sandbox.purged)
            current.next_attempt_at = now + self.config.settle if settle else now
            await tx.sandboxes.update(current)

    async def _failed(self, sandbox: DemoSandbox, step: str, exc: Exception) -> None:
        now = self.deps.clock()
        async with self.deps.store.transaction() as tx:
            current = await tx.sandboxes.get(account_id=sandbox.account_id)
            if current is None:
                return
            current.attempts += 1
            delay = min(
                self.config.retry_max, timedelta(seconds=15 * 2 ** min(current.attempts, 10))
            )
            current.next_attempt_at = now + delay
            current.last_error = f"{step}: {exc!r}"[:500]
            current.purged = list(sandbox.purged)
            await tx.sandboxes.update(current)
        _log.warning(
            "sandbox purge failed, retrying later",
            account_id=str(sandbox.account_id),
            step=step,
            attempts=current.attempts if current else None,
            error=repr(exc),
        )

    async def _purge_identity(self, sandbox: DemoSandbox) -> None:
        """Identity's own rows and tuples of the sandbox, then the row itself."""
        account_id, user_id = sandbox.account_id, sandbox.user_id
        async with self.deps.uow() as work:
            tx = work.tx
            work.account_id = account_id
            found = await tx.users.get(id=user_id)
            await tx.sessions.delete(user_id=user_id)
            await tx.onboarding.delete(user_id=user_id)
            await tx.known_devices.delete(user_id=user_id)
            await tx.api_tokens.delete(account_id=account_id)
            await tx.assignments.delete(account_id=account_id)
            await tx.memberships.delete(account_id=account_id)
            await tx.roles.delete(account_id=account_id)
            await tx.purge_audit(account_id)
            if found is not None:
                await tx.login_attempts.delete(username_norm=found.username_norm)
                await tx.users.delete(id=user_id)
            await tx.accounts.delete(id=account_id)
            await tx.sandboxes.delete(account_id=account_id)
            work.touch(user(user_id))  # nothing is desired any more: every tuple goes
        # The platform tuple is no subject tuple; deleting a missing one is a no-op.
        await self.deps.sync.writer.write([], [platform_tuple(account_id)])
