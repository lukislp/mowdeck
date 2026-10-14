"""The hub of one Realtime instance: open sessions, the mower index and the fan-out.

Every instance receives every ``telemetry.mower.*`` and ``command.result.mower.*`` message
(ephemeral subscriptions without a queue group), so any instance can serve any client. The hub
looks up the sessions subscribed to the message's mower and hands each of them the frame,
encoded once. It never awaits a client: slow clients are handled by their :class:`Outbox`.

Isolation without a database: a subscription exists only after ``can_view`` allowed it, and a
state is additionally handed only to sessions of the state's own account (a third check
without I/O).

Manual driving (ADR 0026): the hub knows the open drive sessions of its sockets, so a rejected
frame result ends the session that sent it, and it keeps the newest capabilities of every
mower (``manual_drive``). Accepted frame results (ten per second) are not fanned out; the
driving client gets ``drive_frame_ack`` instead.
"""

import asyncio
import contextlib
from collections import OrderedDict, defaultdict
from collections.abc import Collection, Coroutine, Iterable
from typing import Any
from uuid import UUID

from mowdeck_contracts.v1 import (
    CommandResult,
    CommandStatus,
    MowerCapabilities,
    MowerCapabilitiesSnapshot,
    MowerState,
    Principal,
    drive_session_of,
    utc_now,
)
from mowdeck_contracts.v1.realtime import CommandResultMessage, RealtimeCloseCode, StateMessage
from mowdeck_service_kit import get_logger
from mowdeck_service_kit.entitlements import Entitlements

from realtime.application.config import RealtimeConfig
from realtime.application.outbox import Frame
from realtime.application.ports import (
    AccessChecker,
    Clock,
    DriveBackend,
    Metrics,
    NoMetrics,
    Socket,
    TokenVerifier,
)
from realtime.application.session import Session, frame_of
from realtime.domain.errors import LimitExceededError

_log = get_logger("realtime.hub")


class Hub:
    """Sessions, subscriptions and the fan-out of one instance."""

    def __init__(
        self,
        config: RealtimeConfig,
        *,
        verifier: TokenVerifier,
        access: AccessChecker,
        clock: Clock = utc_now,
        metrics: Metrics | None = None,
        drive: DriveBackend | None = None,
        plans: Entitlements | None = None,
    ) -> None:
        self.config = config
        self.verifier = verifier
        self.access = access
        self.clock = clock
        self.metrics: Metrics = metrics or NoMetrics()
        self.drive = drive
        """Fleet and the command bus for manual driving; None disables ``drive_start``."""
        self.plans = plans
        """The Entitlements client: ``drive_start`` needs the plan feature ``manual_drive``
        (ADR 0028); None gates nothing."""
        self._drives: dict[UUID, Session] = {}
        self._capabilities: dict[UUID, tuple[UUID, MowerCapabilities]] = {}
        self.sessions: set[Session] = set()
        self._by_account: defaultdict[UUID, set[Session]] = defaultdict(set)
        self._by_user: defaultdict[UUID, set[Session]] = defaultdict(set)
        self._by_mower: defaultdict[UUID, set[Session]] = defaultdict(set)
        self._last_state: OrderedDict[UUID, tuple[UUID, Frame]] = OrderedDict()
        self._subscription_count = 0
        self._tasks: set[asyncio.Task[None]] = set()
        self._idle = asyncio.Event()
        self._idle.set()
        self.accepting = True

    # --- sessions -----------------------------------------------------------------------------

    def open(self, socket: Socket) -> Session:
        """A new session for an accepted socket (closed at once while shutting down)."""
        session = Session(
            self,
            socket,
            config=self.config,
            verifier=self.verifier,
            access=self.access,
            clock=self.clock,
            metrics=self.metrics,
        )
        self.sessions.add(session)
        self._idle.clear()
        self.metrics.connections(len(self.sessions))
        if not self.accepting:
            session.close(RealtimeCloseCode.GOING_AWAY, "going away")
        return session

    def admit(self, session: Session, principal: Principal) -> None:
        """Count the session against the limits of its user and account."""
        assert principal.account_id is not None  # noqa: S101 - checked by the session
        if len(self._by_user[principal.subject]) >= self.config.max_connections_per_user:
            self._forget_empty(principal)
            msg = f"at most {self.config.max_connections_per_user} connections per user"
            raise LimitExceededError(msg)
        if len(self._by_account[principal.account_id]) >= self.config.max_connections_per_account:
            self._forget_empty(principal)
            msg = f"at most {self.config.max_connections_per_account} connections per account"
            raise LimitExceededError(msg)
        self._by_user[principal.subject].add(session)
        self._by_account[principal.account_id].add(session)

    def _forget_empty(self, principal: Principal) -> None:
        if not self._by_user.get(principal.subject):
            self._by_user.pop(principal.subject, None)
        if principal.account_id is not None and not self._by_account.get(principal.account_id):
            self._by_account.pop(principal.account_id, None)

    def remove(self, session: Session) -> None:
        """Forget a finished session everywhere."""
        if session not in self.sessions:
            return
        self.sessions.discard(session)
        self.unsubscribe(session, list(session.subscriptions))
        principal = session.principal
        if principal is not None:
            users = self._by_user.get(principal.subject)
            if users is not None:
                users.discard(session)
            if principal.account_id is not None:
                accounts = self._by_account.get(principal.account_id)
                if accounts is not None:
                    accounts.discard(session)
            self._forget_empty(principal)
        if not self.sessions:
            self._idle.set()
        self.metrics.connections(len(self.sessions))

    def connections_of(self, *, user: UUID | None = None, account: UUID | None = None) -> int:
        """Open authenticated sessions of a user or an account (for tests and metrics)."""
        if user is not None:
            return len(self._by_user.get(user, ()))
        if account is not None:
            return len(self._by_account.get(account, ()))
        return len(self.sessions)

    # --- subscriptions ------------------------------------------------------------------------

    def subscribe(self, session: Session, mower_ids: Iterable[UUID]) -> None:
        """Index the session under the mowers (ids must be allowed already)."""
        for mower_id in mower_ids:
            if mower_id in session.subscriptions:
                continue
            session.subscriptions.add(mower_id)
            self._by_mower[mower_id].add(session)
            self._subscription_count += 1
        self.metrics.subscriptions(self._subscription_count)

    def unsubscribe(self, session: Session, mower_ids: Iterable[UUID]) -> None:
        """Remove the session from the mowers' index."""
        for mower_id in mower_ids:
            if mower_id not in session.subscriptions:
                continue
            session.subscriptions.discard(mower_id)
            subscribers = self._by_mower.get(mower_id)
            if subscribers is not None:
                subscribers.discard(session)
                if not subscribers:
                    del self._by_mower[mower_id]
            self._subscription_count -= 1
        self.metrics.subscriptions(self._subscription_count)

    def subscribers(self, mower_id: UUID) -> int:
        """Sessions subscribed to a mower."""
        return len(self._by_mower.get(mower_id, ()))

    @property
    def subscription_count(self) -> int:
        """Subscribed (session, mower) pairs."""
        return self._subscription_count

    # --- fan-out ------------------------------------------------------------------------------

    def last_state(self, mower_id: UUID, account_id: UUID | None) -> Frame | None:
        """The newest state frame of a mower, if it belongs to ``account_id``."""
        entry = self._last_state.get(mower_id)
        if entry is None or entry[0] != account_id:
            return None
        return entry[1]

    def publish_state(self, state: MowerState) -> None:
        """Hand a state to every subscribed session of the state's account."""
        frame = frame_of(StateMessage(mower_id=state.mower_id, state=state))
        if self.config.last_state_cache_size > 0:
            self._last_state[state.mower_id] = (state.account_id, frame)
            self._last_state.move_to_end(state.mower_id)
            while len(self._last_state) > self.config.last_state_cache_size:
                self._last_state.popitem(last=False)
        for session in tuple(self._by_mower.get(state.mower_id, ())):
            if session.account_id != state.account_id:
                self.metrics.dropped("account_mismatch")
                continue
            session.offer_state(state.mower_id, frame)

    def publish_result(self, result: CommandResult) -> None:
        """Hand a command result to every subscribed session of the mower.

        Results of drive frames end the sending session when the mower refused the frame;
        accepted frame results are not fanned out.
        """
        drive_id = drive_session_of(result.correlation_id)
        if drive_id is not None:
            owner = self._drives.get(drive_id)
            refused = result.status in (CommandStatus.REJECTED, CommandStatus.FAILED)
            if owner is not None and refused:
                self._spawn(owner.drive_refused(drive_id, result), "realtime-drive-refused")
            if result.status is CommandStatus.ACCEPTED:
                return
        subscribers = tuple(self._by_mower.get(result.mower_id, ()))
        if not subscribers:
            return
        frame = frame_of(CommandResultMessage(mower_id=result.mower_id, result=result))
        for session in subscribers:
            session.offer(frame)

    # --- manual driving -----------------------------------------------------------------------

    def publish_capabilities(self, snapshot: MowerCapabilitiesSnapshot) -> None:
        """Remember the newest capabilities of a mower (with its account)."""
        self._capabilities[snapshot.mower_id] = (snapshot.account_id, snapshot.capabilities)

    def capabilities(self, mower_id: UUID, account_id: UUID | None) -> MowerCapabilities | None:
        """The capabilities of a mower of ``account_id``, None when unknown."""
        entry = self._capabilities.get(mower_id)
        if entry is None or entry[0] != account_id:
            return None
        return entry[1]

    def register_drive(self, session_id: UUID, session: Session) -> None:
        """A socket opened a drive session."""
        self._drives[session_id] = session
        self.metrics.drive_sessions(len(self._drives))

    def unregister_drive(self, session_id: UUID) -> None:
        """A drive session ended."""
        self._drives.pop(session_id, None)
        self.metrics.drive_sessions(len(self._drives))

    @property
    def drive_count(self) -> int:
        """Open drive sessions of this instance."""
        return len(self._drives)

    def spawn(self, coroutine: Coroutine[Any, Any, None], name: str) -> None:
        """Run a background job of a session (kept until it finished)."""
        self._spawn(coroutine, name)

    # --- re-checks ----------------------------------------------------------------------------

    def grants_changed(
        self, subjects: Collection[str], *, all_subjects: bool, account_id: UUID | None
    ) -> None:
        """Re-check the sessions named by ``identity.grants.changed`` (in the background)."""
        if all_subjects:
            targets = [
                s
                for s in self.sessions
                if s.authenticated and (account_id is None or s.account_id == account_id)
            ]
        else:
            wanted = set(subjects)
            targets = [s for s in self.sessions if s.subject_key in wanted]
        if targets:
            self._spawn(self._recheck(targets), "realtime-recheck-grants")

    async def recheck_all(self) -> None:
        """Re-check every session (the periodic safety net)."""
        await self._recheck([s for s in self.sessions if s.subscriptions or s.drive is not None])

    async def _recheck(self, sessions: list[Session]) -> None:
        if not sessions:
            return
        if not await self.access.available():
            _log.warning("authorization unavailable, re-check skipped", sessions=len(sessions))
            return
        for session in sessions:
            try:
                await session.recheck()
            except Exception:
                _log.exception("re-check failed", session=str(session.id))

    async def run_rechecks(self) -> None:
        """Re-check every ``recheck_seconds`` until cancelled."""
        while True:
            await asyncio.sleep(self.config.recheck_seconds)
            try:
                await self.recheck_all()
            except Exception:
                _log.exception("periodic re-check failed")

    def _spawn(self, coroutine: Coroutine[Any, Any, None], name: str) -> None:
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # --- shutdown -----------------------------------------------------------------------------

    async def close_all(
        self,
        code: int = RealtimeCloseCode.GOING_AWAY,
        reason: str = "going away",
        *,
        timeout: float = 5.0,  # noqa: ASYNC109 - a bound for the whole shutdown step
    ) -> None:
        """Stop accepting, close every session and wait (bounded) until they are gone."""
        self.accepting = False
        for task in tuple(self._tasks):
            task.cancel()
        for session in tuple(self.sessions):
            session.close(code, reason, flush=False)
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(timeout):
                await self._idle.wait()
        if self.sessions:
            _log.warning("sessions still open after close", count=len(self.sessions))
