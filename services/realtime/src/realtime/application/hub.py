"""The hub of one Realtime instance: open sessions, the mower index and the fan-out.

Every instance receives every ``telemetry.mower.*`` and ``command.result.mower.*`` message
(ephemeral subscriptions without a queue group), so any instance can serve any client. The hub
looks up the sessions subscribed to the message's mower and hands each of them the frame,
encoded once. It never awaits a client: slow clients are handled by their :class:`Outbox`.

Isolation without a database: a subscription exists only after ``can_view`` allowed it, and a
state is additionally handed only to sessions of the state's own account (a third check
without I/O).

Connection limits (4429): per user or API token, per client address (optional) and per
account. In the public demo (ADR 0068) every visitor is the same user, so the user limit counts
per login session (``sid``) instead, and the per-address limit keeps one visitor from taking
every socket of the shared account.

Manual driving (ADR 0026): the hub knows the open drive sessions of its sockets, so a rejected
frame result ends the session that sent it, and it keeps the newest capabilities of every
mower (``manual_drive``). Accepted frame results (ten per second) are not fanned out; the
driving client gets ``drive_frame_ack`` instead.

Notifications and AI assist proposals (``notification``, ``vision_proposal``):

* ``notifications.sent`` is one item of a person's inbox; Notifications already checked the
  person's preferences and access. It goes to every session of that person (``user`` or
  ``subuser`` principal with the same subject and account), without a subscription.
* ``notifications.raised`` is the account's copy for API tokens, which have no inbox. It goes to
  the API-token sessions of the account whose token holds ``notifications.view``; one about a
  mower only to sessions subscribed to it (``can_view`` checked and re-checked like states), and
  for the AI assist kinds (``required_relation`` ``can_drive``) only after ``can_drive``.
* ``vision.proposal.created`` / ``.updated`` go to the sessions subscribed to the proposal's
  mower within its account, exactly like states (the proposal API needs ``can_view`` too).
"""

import asyncio
import contextlib
from collections import OrderedDict, defaultdict
from collections.abc import Collection, Coroutine, Iterable
from typing import Any
from uuid import UUID, uuid4

from mowdeck_contracts.v1 import (
    NOTIFICATIONS_VIEW_PERMISSION,
    VISION_FINAL_STATES,
    AccountPresence,
    AccountWatchers,
    CommandResult,
    CommandStatus,
    MowerCapabilities,
    MowerCapabilitiesSnapshot,
    MowerState,
    NotificationRaised,
    NotificationSent,
    Principal,
    PrincipalKind,
    VisionProposalCreated,
    VisionProposalState,
    VisionProposalUpdated,
    drive_session_of,
    utc_now,
)
from mowdeck_contracts.v1.realtime import (
    CommandResultMessage,
    NotificationMessage,
    NotificationScope,
    RealtimeCloseCode,
    RealtimeNotification,
    StateMessage,
    VisionProposalEvent,
    VisionProposalMessage,
    VisionProposalSummary,
)
from mowdeck_service_kit import get_logger
from mowdeck_service_kit.client_address import limiter_key
from mowdeck_service_kit.entitlements import Entitlements

from realtime.application.config import RealtimeConfig
from realtime.application.outbox import Frame
from realtime.application.ports import (
    AccessChecker,
    Clock,
    DriveBackend,
    Metrics,
    NoMetrics,
    PresencePublisher,
    Socket,
    TokenVerifier,
)
from realtime.application.session import Session, frame_of
from realtime.domain.errors import LimitExceededError

_log = get_logger("realtime.hub")

PERSON_KINDS = frozenset({PrincipalKind.USER, PrincipalKind.SUBUSER})
"""Principals with an inbox of their own."""


def receives_account_notifications(principal: Principal | None) -> bool:
    """An API token that holds ``notifications.view`` (people get their own inbox items)."""
    return (
        principal is not None
        and principal.kind is PrincipalKind.API_TOKEN
        and principal.has_permission(NOTIFICATIONS_VIEW_PERMISSION)
    )


def _discard[K](index: defaultdict[K, set[Session]], key: K, session: Session) -> None:
    """Remove ``session`` from ``index[key]`` and drop the key once it is empty."""
    sessions = index.get(key)
    if sessions is None:
        return
    sessions.discard(session)
    if not sessions:
        del index[key]


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
        presence: PresencePublisher | None = None,
        instance: str | None = None,
    ) -> None:
        self.config = config
        self.presence = presence if config.presence_seconds > 0 else None
        """Announces the watched accounts (demo sandboxes, ADR 0076); None announces nothing."""
        self.instance = instance or uuid4().hex
        """Names this instance in its presence announcements."""
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
        """Sessions of a person (inbox delivery)."""
        self._by_owner: defaultdict[tuple[str, UUID], set[Session]] = defaultdict(set)
        """Sessions counted against the user limit (see :meth:`_owner_key`)."""
        self._by_address: defaultdict[str, set[Session]] = defaultdict(set)
        """Sessions counted against the per-address limit."""
        self._by_mower: defaultdict[UUID, set[Session]] = defaultdict(set)
        self._last_state: OrderedDict[UUID, tuple[UUID, Frame]] = OrderedDict()
        self._subscription_count = 0
        self._tasks: set[asyncio.Task[None]] = set()
        self._idle = asyncio.Event()
        self._idle.set()
        self.accepting = True

    # --- sessions -----------------------------------------------------------------------------

    def open(self, socket: Socket, *, address: str | None = None) -> Session:
        """A new session for an accepted socket (closed at once while shutting down);
        ``address`` is the client address of the handshake; the per-address limit counts its
        :func:`~mowdeck_service_kit.client_address.limiter_key` (IPv6 per /64)."""
        session = Session(
            self,
            socket,
            config=self.config,
            verifier=self.verifier,
            access=self.access,
            clock=self.clock,
            metrics=self.metrics,
        )
        session.address = limiter_key(address) if address else None
        self.sessions.add(session)
        self._idle.clear()
        self.metrics.connections(len(self.sessions))
        if not self.accepting:
            session.close(RealtimeCloseCode.GOING_AWAY, "going away")
        return session

    def _owner_key(self, principal: Principal) -> tuple[str, UUID]:
        """What the user limit counts: the user (or API token), or in the public demo, where
        every visitor is the same user, the login session (``sid``)."""
        if self.config.limit_per_login_session and principal.session_id is not None:
            return ("session", principal.session_id)
        return ("subject", principal.subject)

    def admit(self, session: Session, principal: Principal) -> None:
        """Count the session against the limits of its user (or login session), its client
        address and its account."""
        assert principal.account_id is not None  # noqa: S101 - checked by the session
        owner = self._owner_key(principal)
        per_owner = self.config.max_connections_per_user
        if len(self._by_owner.get(owner, ())) >= per_owner:
            unit = "login session" if owner[0] == "session" else "user"
            raise LimitExceededError(f"at most {per_owner} connections per {unit}")
        per_address = self.config.max_connections_per_address
        address = session.address
        if per_address and address and len(self._by_address.get(address, ())) >= per_address:
            raise LimitExceededError(f"at most {per_address} connections per address")
        per_account = self.config.max_connections_per_account
        if len(self._by_account.get(principal.account_id, ())) >= per_account:
            raise LimitExceededError(f"at most {per_account} connections per account")
        session.owner_key = owner
        self._by_owner[owner].add(session)
        if address:
            self._by_address[address].add(session)
        self._by_user[principal.subject].add(session)
        first = principal.account_id not in self._by_account
        self._by_account[principal.account_id].add(session)
        if first:
            self._announce({principal.account_id: 1})

    def remove(self, session: Session) -> None:
        """Forget a finished session everywhere."""
        if session not in self.sessions:
            return
        self.sessions.discard(session)
        self.unsubscribe(session, list(session.subscriptions))
        principal = session.principal
        if principal is not None:
            _discard(self._by_user, principal.subject, session)
            account_id = principal.account_id
            if account_id is not None and session in self._by_account.get(account_id, ()):
                _discard(self._by_account, account_id, session)
                if account_id not in self._by_account:
                    self._announce({account_id: 0})
        if session.owner_key is not None:
            _discard(self._by_owner, session.owner_key, session)
        if session.address:
            _discard(self._by_address, session.address, session)
        if not self.sessions:
            self._idle.set()
        self.metrics.connections(len(self.sessions))

    def connections_of(
        self,
        *,
        user: UUID | None = None,
        account: UUID | None = None,
        address: str | None = None,
    ) -> int:
        """Open authenticated sessions of a user, an account or a client address (for tests and
        metrics)."""
        if user is not None:
            return len(self._by_user.get(user, ()))
        if account is not None:
            return len(self._by_account.get(account, ()))
        if address is not None:
            return len(self._by_address.get(limiter_key(address), ()))
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

    # --- notifications and AI assist ------------------------------------------------------------

    def publish_user_notification(self, sent: NotificationSent) -> None:
        """Hand an inbox item to every session of its person (no subscription needed)."""
        sessions = tuple(self._by_user.get(sent.user_id, ()))
        if not sessions:
            return
        frame = frame_of(
            NotificationMessage(
                notification=RealtimeNotification(
                    id=sent.notification_id,
                    scope=NotificationScope.USER,
                    mower_id=sent.mower_id,
                    kind=sent.kind,
                    severity=sent.severity,
                    title=sent.title,
                    body=sent.body,
                    url=sent.url,
                    occurred_at=sent.occurred_at,
                    created_at=sent.created_at or sent.occurred_at,
                )
            )
        )
        for session in sessions:
            principal = session.principal
            if (
                principal is None
                or principal.account_id != sent.account_id
                or principal.kind not in PERSON_KINDS
            ):
                self.metrics.dropped("account_mismatch")
                continue
            session.offer(frame)

    def publish_account_notification(self, raised: NotificationRaised) -> None:
        """Hand the account's notification to its API-token sessions with
        ``notifications.view`` (and the mower subscribed; ``can_drive`` where required)."""
        targets = [
            session
            for session in tuple(self._by_account.get(raised.account_id, ()))
            if receives_account_notifications(session.principal)
            and session.account_id == raised.account_id
            and (raised.mower_id is None or raised.mower_id in session.subscriptions)
        ]
        if not targets:
            return
        frame = frame_of(
            NotificationMessage(
                notification=RealtimeNotification(
                    id=raised.notification_id,
                    scope=NotificationScope.ACCOUNT,
                    mower_id=raised.mower_id,
                    kind=raised.kind,
                    severity=raised.severity,
                    title=raised.title,
                    body=raised.body,
                    url=raised.url,
                    occurred_at=raised.occurred_at,
                    created_at=raised.created_at,
                )
            )
        )
        if raised.required_relation == "can_drive" and raised.mower_id is not None:
            self._spawn(
                self._offer_to_drivers(targets, raised.mower_id, frame),
                "realtime-notification-drivers",
            )
            return
        for session in targets:
            session.offer(frame)

    async def _offer_to_drivers(
        self, sessions: list[Session], mower_id: UUID, frame: Frame
    ) -> None:
        for session in sessions:
            principal = session.principal
            if principal is None or session.outbox.closing:
                continue
            if await self.access.can_drive(principal, mower_id):
                session.offer(frame)

    def publish_proposal(self, event: VisionProposalCreated | VisionProposalUpdated) -> None:
        """Hand a proposal summary to the subscribed sessions of the proposal's account."""
        subscribers = tuple(self._by_mower.get(event.mower_id, ()))
        if not subscribers:
            return
        frame = frame_of(proposal_message(event))
        for session in subscribers:
            if session.account_id != event.account_id:
                self.metrics.dropped("account_mismatch")
                continue
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

    # --- presence (demo sandboxes, ADR 0076) ---------------------------------------------------

    def watched_accounts(self) -> dict[UUID, int]:
        """Open authenticated sockets per account of this instance."""
        return {account: len(sessions) for account, sessions in self._by_account.items()}

    def _announce(self, accounts: dict[UUID, int], *, full: bool = False) -> None:
        """Announce in the background (never blocks a socket on the bus)."""
        if self.presence is None:
            return
        self._spawn(self._send_presence(accounts, full=full), "realtime-presence")

    async def _send_presence(self, accounts: dict[UUID, int], *, full: bool) -> None:
        if self.presence is None:
            return
        await self.presence.announce(
            AccountPresence(
                instance=self.instance,
                accounts=[
                    AccountWatchers(account_id=account, watchers=count)
                    for account, count in accounts.items()
                ],
                full=full,
                interval_seconds=self.config.presence_seconds,
                at=self.clock(),
            )
        )

    async def run_presence(self) -> None:
        """Announce every watched account as a keep-alive every ``presence_seconds`` until
        cancelled (an empty list too: it tells a restarted listener that nobody watches)."""
        if self.presence is None:
            return
        while True:
            await asyncio.sleep(self.config.presence_seconds)
            try:
                await self._send_presence(self.watched_accounts(), full=True)
            except Exception:
                _log.exception("presence keep-alive failed")

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


def proposal_message(
    event: VisionProposalCreated | VisionProposalUpdated,
) -> VisionProposalMessage:
    """The ``vision_proposal`` message of a Vision event (a summary, never images)."""
    if isinstance(event, VisionProposalCreated):
        summary = VisionProposalSummary(
            id=event.proposal_id,
            mower_id=event.mower_id,
            task=event.task,
            round=event.round,
            state=VisionProposalState.PROPOSED,
            final=False,
            action=event.action,
            steps=event.steps,
            fallback=event.fallback,
            low_confidence=event.low_confidence,
            alternative_action=event.alternative_action,
            confidence=event.confidence,
            situation=event.situation,
            trigger=event.trigger,
            error_code=event.error_code,
            expires_at=event.expires_at,
            link=event.link,
            updated_at=event.created_at,
        )
        kind = VisionProposalEvent.CREATED
    else:
        summary = VisionProposalSummary(
            id=event.proposal_id,
            mower_id=event.mower_id,
            task=event.task,
            round=event.round,
            state=event.state,
            final=event.final or event.state in VISION_FINAL_STATES,
            action=event.action,
            steps=event.steps,
            fallback=event.fallback,
            low_confidence=event.low_confidence,
            alternative_action=event.alternative_action,
            trigger=event.trigger,
            error_code=event.error_code,
            photo_problem=event.photo_problem,
            signal_dbm=event.signal_dbm,
            expires_at=event.expires_at,
            link=event.link,
            updated_at=event.updated_at,
        )
        kind = VisionProposalEvent.UPDATED
    return VisionProposalMessage(event=kind, mower_id=event.mower_id, proposal=summary)
