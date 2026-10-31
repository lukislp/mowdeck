"""One WebSocket connection: authentication, subscriptions, re-checks and the writer.

Life of a session (``serve``):

1. The first frame must be ``auth`` within ``auth_timeout_seconds``; otherwise, or with an
   invalid token, a service principal or a principal without account, the socket closes with
   4401. A connection limit of the user or account closes it with 4429.
2. ``ready`` confirms the principal and the token expiry. At the expiry the socket closes with
   4401 unless a fresh ``auth`` (same subject and account) arrived; a fresh token also re-checks
   every subscription.
3. ``subscribe`` asks ``can_view`` for every id in one batch; allowed ids are subscribed and
   get the newest known state, the others are reported in ``denied`` and never subscribed.
4. Re-checks (``identity.grants.changed`` for the subject, the periodic timer, a token refresh)
   drop ids the principal may no longer view and report them in ``revoked``.
5. Manual driving (ADR 0026): ``drive_start`` checks the plan feature ``manual_drive``
   (ADR 0028), ``can_drive`` and the mower's
   capabilities and asks Fleet for a session; each ``drive_frame`` (rate limited) becomes a
   ``ManualDrive`` command on ``command.mower.<id>``; ``drive_stop``, a closed socket, a
   revocation, the token expiry, the maximum duration or a refused frame end the session with
   a zero frame and ``fleet.drive.stop``. At most one drive session per socket.

Two tasks run per session: the reader (this module's ``_read``) and the writer
(``_write``), which owns the socket's sending side. The bus never waits for a client: the hub
only puts frames into the :class:`Outbox`.
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from mowdeck_contracts.v1 import (
    CommandResult,
    DriveEndReason,
    DriveErrorCode,
    DriveStartRequest,
    DriveStopRequest,
    Principal,
)
from mowdeck_contracts.v1.realtime import (
    AuthMessage,
    DriveCapabilities,
    DriveEndedMessage,
    DriveFrameAckMessage,
    DriveFrameMessage,
    DriveStartedMessage,
    DriveStartMessage,
    DriveStopMessage,
    ErrorMessage,
    PingMessage,
    PongMessage,
    PrincipalSummary,
    ReadyMessage,
    RealtimeClientMessage,
    RealtimeCloseCode,
    RealtimeErrorCode,
    RevokedMessage,
    RevokeReason,
    SubscribedMessage,
    SubscribeMessage,
    UnsubscribedMessage,
    UnsubscribeMessage,
)
from mowdeck_service_kit import get_logger
from pydantic import BaseModel, ValidationError

from realtime.application.config import RealtimeConfig
from realtime.application.drive import ActiveDrive, TokenBucket, frame_envelope
from realtime.application.outbox import Frame, Outbox
from realtime.application.ports import AccessChecker, Clock, Metrics, Socket, TokenVerifier
from realtime.domain.errors import (
    DriveUnavailableError,
    FrameRejectedError,
    InvalidTokenError,
    LimitExceededError,
)
from realtime.domain.models import (
    CloseRequest,
    acceptable_principal,
    close_reason,
    same_principal,
    unique_ids,
)

if TYPE_CHECKING:
    from realtime.application.hub import Hub

_log = get_logger("realtime.session")

ClientRequest = (
    AuthMessage
    | SubscribeMessage
    | UnsubscribeMessage
    | PingMessage
    | DriveStartMessage
    | DriveFrameMessage
    | DriveStopMessage
)
"""A parsed client message."""

_DRIVE_ERRORS = {
    DriveErrorCode.NOT_FOUND: RealtimeErrorCode.DRIVE_NOT_ALLOWED,
    DriveErrorCode.FORBIDDEN: RealtimeErrorCode.DRIVE_NOT_ALLOWED,
    DriveErrorCode.INVALID: RealtimeErrorCode.DRIVE_NOT_ALLOWED,
    DriveErrorCode.BUSY: RealtimeErrorCode.DRIVE_BUSY,
}
_BUSY_HINT = "pause or end the job"

Receive = Callable[[], Awaitable[str | None]]
"""Returns the next text frame, None when the client went away; raises
:class:`FrameRejectedError` for a frame that ends the connection."""


def frame_of(message: BaseModel) -> Frame:
    """Encode a server message once."""
    message_type = str(getattr(message, "type", "unknown"))
    return Frame(message_type, message.model_dump_json())


class Session:
    """The server side of one socket."""

    def __init__(
        self,
        hub: "Hub",
        socket: Socket,
        *,
        config: RealtimeConfig,
        verifier: TokenVerifier,
        access: AccessChecker,
        clock: Clock,
        metrics: Metrics,
    ) -> None:
        self.id: UUID = uuid4()
        self.hub = hub
        self.socket = socket
        self.config = config
        self.verifier = verifier
        self.access = access
        self.clock = clock
        self.metrics = metrics
        self.outbox = Outbox(config.queue_size)
        self.principal: Principal | None = None
        self.subject_key: str | None = None
        self.subscriptions: set[UUID] = set()
        self.drive: ActiveDrive | None = None
        """The open drive session of this socket, if any."""
        self._expiry: asyncio.Task[None] | None = None
        self.closed_with: int | None = None
        self.address: str | None = None
        """Client address of the handshake (the hub's per-address limit)."""
        self.owner_key: tuple[str, UUID] | None = None
        """What the hub counted this session against for the user limit (set on admission)."""

    # --- identity -----------------------------------------------------------------------------

    @property
    def account_id(self) -> UUID | None:
        """The account of the authenticated principal."""
        return self.principal.account_id if self.principal is not None else None

    @property
    def authenticated(self) -> bool:
        """True after the first accepted ``auth``."""
        return self.principal is not None

    # --- lifecycle ----------------------------------------------------------------------------

    async def serve(self, receive: Receive) -> None:
        """Run until the socket is closed by either side."""
        writer = asyncio.create_task(self._write(), name=f"realtime-writer-{self.id}")
        reader = asyncio.create_task(self._read(receive), name=f"realtime-reader-{self.id}")
        try:
            await writer
        finally:
            for task in (reader, writer, self._expiry):
                if task is not None and not task.done():
                    task.cancel()
            for task in (reader, writer, self._expiry):
                if task is not None:
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
            if self.drive is not None:
                with contextlib.suppress(Exception):
                    await self.end_drive(self.drive, DriveEndReason.SOCKET_CLOSED, notify=False)
            self.hub.remove(self)

    def close(self, code: int, reason: str, *, flush: bool = True) -> None:
        """End the session with a close frame (after the queued messages when ``flush``)."""
        self.outbox.close(CloseRequest(code, close_reason(reason), flush=flush))

    def gone(self) -> None:
        """The client went away: stop without sending anything."""
        self.outbox.close(CloseRequest(None))

    def send(self, message: BaseModel) -> None:
        """Queue a server message (a full queue closes the socket with 4430)."""
        self.offer(frame_of(message))

    def offer_state(self, mower_id: UUID, frame: Frame) -> None:
        """Put the newest state of a subscribed mower (never waits)."""
        if self.outbox.offer_state(mower_id, frame):
            self.metrics.dropped("coalesced")

    def offer(self, frame: Frame) -> None:
        """Queue an already encoded frame (fan-out of command results)."""
        was_closing = self.outbox.closing
        if not self.outbox.offer(frame) and not was_closing:
            self.metrics.dropped("slow_consumer")
            _log.info("slow consumer closed", session=str(self.id))

    # --- writer -------------------------------------------------------------------------------

    async def _write(self) -> None:
        """Send frames until a close is requested; owns the sending side of the socket."""
        outbox = self.outbox
        while True:
            await outbox.wait()
            request = outbox.close_request
            if request is not None:
                await self._finish(request)
                return
            frame = outbox.next_frame()
            if frame is None:
                continue
            try:
                async with asyncio.timeout(self.config.send_timeout_seconds):
                    await self.socket.send_text(frame.text)
            except TimeoutError:
                self.metrics.dropped("slow_consumer")
                self.close(RealtimeCloseCode.SLOW_CONSUMER, "slow consumer", flush=False)
                continue
            except Exception:  # the peer is gone (disconnect, reset)
                self.gone()
                continue
            self.metrics.sent(frame.type)

    async def _finish(self, request: CloseRequest) -> None:
        self.closed_with = request.code
        self.metrics.closed(request.code)
        if request.code is None:
            return
        try:
            async with asyncio.timeout(self.config.close_timeout_seconds):
                if request.flush:
                    for frame in self.outbox.drain_queue():
                        await self.socket.send_text(frame.text)
                        self.metrics.sent(frame.type)
                await self.socket.close(request.code, request.reason)
        except Exception as exc:
            _log.debug("close failed", error=repr(exc), session=str(self.id))

    # --- reader -------------------------------------------------------------------------------

    async def _read(self, receive: Receive) -> None:
        try:
            await self._read_frames(receive)
        except FrameRejectedError as exc:
            self.close(exc.code, exc.message, flush=False)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("realtime session failed", session=str(self.id))
            self.send(ErrorMessage(code=RealtimeErrorCode.INTERNAL, message="internal error"))
            self.close(1011, "internal error")

    async def _read_frames(self, receive: Receive) -> None:
        try:
            async with asyncio.timeout(self.config.auth_timeout_seconds):
                first = await receive()
        except TimeoutError:
            self.close(RealtimeCloseCode.UNAUTHORIZED, "authentication timeout")
            return
        if first is None:
            self.gone()
            return
        message = self._parse(first)
        if not isinstance(message, AuthMessage):
            self._reject("the first message must be auth")
            return
        if not await self._authenticate(message):
            return
        while not self.outbox.closing:
            raw = await receive()
            if raw is None:
                self.gone()
                return
            parsed = self._parse(raw)
            if parsed is not None:
                await self._handle(parsed)

    def _parse(self, raw: str) -> ClientRequest | None:
        if len(raw.encode("utf-8")) > self.config.max_message_bytes:
            raise FrameRejectedError(RealtimeCloseCode.MESSAGE_TOO_BIG, "message too big")
        try:
            return RealtimeClientMessage.model_validate_json(raw).root
        except ValidationError:
            self.metrics.dropped("invalid")
            if self.authenticated:
                self.send(
                    ErrorMessage(
                        code=RealtimeErrorCode.INVALID_MESSAGE,
                        message="not a valid client message",
                    )
                )
            return None

    async def _handle(self, message: ClientRequest) -> None:
        match message:
            case AuthMessage():
                await self._authenticate(message)
            case SubscribeMessage():
                await self._subscribe(message)
            case UnsubscribeMessage():
                self._unsubscribe(message)
            case PingMessage():
                self.send(PongMessage(request_id=message.request_id))
            case DriveStartMessage():
                await self._drive_start(message)
            case DriveFrameMessage():
                await self._drive_frame(message)
            case DriveStopMessage():
                await self._drive_stop(message)

    # --- authentication -----------------------------------------------------------------------

    def _reject(self, reason: str) -> None:
        self.send(ErrorMessage(code=RealtimeErrorCode.UNAUTHORIZED, message=reason))
        self.close(RealtimeCloseCode.UNAUTHORIZED, reason)

    async def _authenticate(self, message: AuthMessage) -> bool:
        try:
            verified = await self.verifier.verify(message.token)
        except InvalidTokenError:
            self._reject("invalid or expired access token")
            return False
        principal = verified.principal
        if not acceptable_principal(principal):
            self._reject("this principal cannot subscribe")
            return False
        if self.principal is not None and not same_principal(self.principal, principal):
            self._reject("the token belongs to another principal")
            return False
        if verified.expires_at <= self.clock():
            self._reject("token expired")
            return False
        first = self.principal is None
        if first:
            try:
                self.hub.admit(self, principal)
            except LimitExceededError as exc:
                self.send(ErrorMessage(code=RealtimeErrorCode.LIMIT_EXCEEDED, message=exc.message))
                self.close(RealtimeCloseCode.LIMIT_EXCEEDED, exc.message)
                return False
            self.subject_key = self.access.subject_key(principal)
        self.principal = principal
        self._schedule_expiry(verified.expires_at)
        assert principal.account_id is not None  # noqa: S101 - acceptable_principal checked it
        self.send(
            ReadyMessage(
                connection_id=self.id,
                principal=PrincipalSummary(
                    subject=principal.subject,
                    account_id=principal.account_id,
                    kind=principal.kind,
                    display_name=principal.display_name,
                ),
                token_expires_at=verified.expires_at,
                server_time=self.clock(),
            )
        )
        if not first:
            await self.recheck()
        return True

    def _schedule_expiry(self, expires_at: datetime) -> None:
        if self._expiry is not None:
            self._expiry.cancel()
        delay = (expires_at - self.clock()).total_seconds()

        async def expire() -> None:
            await asyncio.sleep(max(delay, 0.0))
            if self.drive is not None:
                await self.end_drive(self.drive, DriveEndReason.TOKEN_EXPIRED)
            self._reject("token expired")

        self._expiry = asyncio.create_task(expire(), name=f"realtime-expiry-{self.id}")

    # --- subscriptions ------------------------------------------------------------------------

    async def _subscribe(self, message: SubscribeMessage) -> None:
        assert self.principal is not None  # noqa: S101 - only called after authentication
        principal = self.principal
        ids = unique_ids(message.mower_ids)
        if len(self.subscriptions | set(ids)) > self.config.max_subscriptions:
            reason = f"at most {self.config.max_subscriptions} subscriptions per connection"
            self.send(
                ErrorMessage(
                    request_id=message.request_id,
                    code=RealtimeErrorCode.LIMIT_EXCEEDED,
                    message=reason,
                )
            )
            self.close(RealtimeCloseCode.LIMIT_EXCEEDED, reason)
            return
        answers = await self.access.can_view(principal, ids)
        if self.outbox.closing:
            return
        granted = [mower_id for mower_id, ok in zip(ids, answers, strict=True) if ok]
        denied = [mower_id for mower_id, ok in zip(ids, answers, strict=True) if not ok]
        self._drop(denied)
        self.hub.subscribe(self, granted)
        self.send(
            SubscribedMessage(request_id=message.request_id, mower_ids=granted, denied=denied)
        )
        for mower_id in granted:
            frame = self.hub.last_state(mower_id, principal.account_id)
            if frame is not None:
                self.offer_state(mower_id, frame)

    def _unsubscribe(self, message: UnsubscribeMessage) -> None:
        ids = unique_ids(message.mower_ids)
        self._drop(ids)
        self.send(UnsubscribedMessage(request_id=message.request_id, mower_ids=ids))

    def _drop(self, mower_ids: list[UUID]) -> None:
        present = [m for m in mower_ids if m in self.subscriptions]
        if present:
            self.hub.unsubscribe(self, present)
        self.outbox.discard_states(mower_ids)

    async def recheck(self) -> None:
        """Ask ``can_view`` again for every subscription; revoke what is no longer allowed.

        An open drive session is checked with ``can_drive`` and ends with ``revoked``.
        """
        principal = self.principal
        if principal is None or self.outbox.closing:
            return
        drive = self.drive
        if drive is not None and not await self.access.can_drive(principal, drive.mower_id):
            await self.end_drive(drive, DriveEndReason.REVOKED, detail="can_drive was revoked")
        if not self.subscriptions or self.outbox.closing:
            return
        ids = sorted(self.subscriptions)
        answers = await self.access.can_view(principal, ids)
        revoked = [
            mower_id
            for mower_id, ok in zip(ids, answers, strict=True)
            if not ok and mower_id in self.subscriptions
        ]
        if not revoked or self.outbox.closing:
            return
        self._drop(revoked)
        self.send(RevokedMessage(mower_ids=revoked, reason=RevokeReason.ACCESS_REVOKED))
        _log.info("subscriptions revoked", session=str(self.id), count=len(revoked))

    # --- manual driving -----------------------------------------------------------------------

    def _drive_error(
        self, code: RealtimeErrorCode, message: str, request_id: str | None = None
    ) -> None:
        self.send(ErrorMessage(request_id=request_id, code=code, message=message[:500]))

    async def _drive_start(self, message: DriveStartMessage) -> None:
        """Check ``can_drive`` and the capabilities, ask Fleet, then accept frames."""
        assert self.principal is not None  # noqa: S101 - only called after authentication
        principal = self.principal
        request_id = message.request_id
        if self.drive is not None:
            await self.end_drive(self.drive, DriveEndReason.REPLACED)
        backend = self.hub.drive
        if backend is None:
            self._drive_error(
                RealtimeErrorCode.DRIVE_UNAVAILABLE, "manual driving is not available", request_id
            )
            return
        plans = self.hub.plans
        if plans is not None and principal.account_id is not None:
            in_plan = await plans.check_feature(principal.account_id, "manual_drive")
            if not in_plan.allowed:
                self._drive_error(
                    RealtimeErrorCode.DRIVE_NOT_ALLOWED,
                    "the plan of the account does not include manual driving",
                    request_id,
                )
                return
        if not await self.access.can_drive(principal, message.mower_id):
            self._drive_error(
                RealtimeErrorCode.DRIVE_NOT_ALLOWED, "not allowed to drive this mower", request_id
            )
            return
        capabilities = self.hub.capabilities(message.mower_id, principal.account_id)
        if capabilities is None or not capabilities.manual_drive:
            self._drive_error(
                RealtimeErrorCode.DRIVE_UNSUPPORTED,
                "the mower offers no manual driving over its connection",
                request_id,
            )
            return
        assert principal.account_id is not None  # noqa: S101 - acceptable_principal checked it
        blades = message.blade_on_allowed and capabilities.manual_drive_blades
        try:
            reply = await backend.start(
                DriveStartRequest(
                    on_behalf_of=principal.ref(),
                    account_id=principal.account_id,
                    mower_id=message.mower_id,
                    blade_on_allowed=blades,
                    max_seconds=self.config.drive_max_seconds,
                    connection_id=self.id,
                )
            )
        except DriveUnavailableError as exc:
            self._drive_error(RealtimeErrorCode.DRIVE_UNAVAILABLE, exc.message, request_id)
            return
        if reply.error is not None:
            self._drive_error(_DRIVE_ERRORS[reply.error.code], reply.error.detail, request_id)
            return
        started = reply.started
        assert started is not None  # noqa: S101 - exactly one of started or error
        drive = ActiveDrive(
            session_id=started.session_id,
            mower_id=started.mower_id,
            account_id=started.account_id,
            blade_on_allowed=blades,
            capabilities=DriveCapabilities(
                manual_drive=capabilities.manual_drive,
                manual_drive_blades=capabilities.manual_drive_blades,
            ),
            started_at=started.started_at,
            limiter=TokenBucket(
                self.config.drive_max_rate_hz,
                max(1.0, self.config.drive_max_rate_hz / 5),
                asyncio.get_running_loop().time(),
            ),
        )
        self.drive = drive
        self.hub.register_drive(drive.session_id, self)
        if self.outbox.closing:  # the socket went away while Fleet answered
            await self.end_drive(drive, DriveEndReason.SOCKET_CLOSED, notify=False)
            return
        drive.timer = asyncio.create_task(
            self._drive_deadline(drive), name=f"realtime-drive-{drive.session_id}"
        )
        self.send(
            DriveStartedMessage(
                request_id=request_id,
                session_id=drive.session_id,
                mower_id=drive.mower_id,
                max_rate_hz=self.config.drive_max_rate_hz,
                ttl_ms=self.config.drive_ttl_ms,
                max_seconds=self.config.drive_max_seconds,
                blade_on_allowed=blades,
                capabilities=drive.capabilities,
            )
        )
        _log.info(
            "drive session started",
            session=str(self.id),
            drive_session=str(drive.session_id),
            mower_id=str(drive.mower_id),
        )

    async def _drive_deadline(self, drive: ActiveDrive) -> None:
        await asyncio.sleep(self.config.drive_max_seconds)
        if self.drive is drive:
            drive.timer = None  # this task is ending; end_drive must not cancel it
            await self.end_drive(drive, DriveEndReason.MAX_DURATION)

    async def _drive_frame(self, message: DriveFrameMessage) -> None:
        """Publish one frame as a ``ManualDrive`` command (rate limited)."""
        drive = self.drive
        backend = self.hub.drive
        if drive is None or backend is None or drive.session_id != message.session_id:
            self._drive_error(RealtimeErrorCode.DRIVE_NOT_ACTIVE, "no such drive session")
            return
        if not drive.limiter.allow(asyncio.get_running_loop().time()):
            drive.dropped += 1
            self.metrics.drive_frame("rate_limited")
            return
        assert self.principal is not None  # noqa: S101 - only called after authentication
        envelope = frame_envelope(
            drive,
            self.principal.ref(),
            linear=message.linear,
            angular=message.angular,
            blade_on=message.blade_on,
            ttl_ms=self.config.drive_ttl_ms,
            now=self.clock(),
        )
        try:
            await backend.publish(envelope)
        except Exception as exc:
            self.metrics.drive_frame("failed")
            _log.warning("drive frame not published", error=repr(exc), session=str(self.id))
            self._drive_error(RealtimeErrorCode.DRIVE_UNAVAILABLE, "the frame was not sent")
            return
        drive.frames += 1
        drive.last_frame_at = asyncio.get_running_loop().time()
        self.metrics.drive_frame("published")
        if message.seq is not None:
            self.send(DriveFrameAckMessage(session_id=drive.session_id, seq=message.seq))

    async def _drive_stop(self, message: DriveStopMessage) -> None:
        drive = self.drive
        if drive is None or drive.session_id != message.session_id:
            self._drive_error(
                RealtimeErrorCode.DRIVE_NOT_ACTIVE, "no such drive session", message.request_id
            )
            return
        await self.end_drive(drive, DriveEndReason.CLIENT_STOP)

    async def drive_refused(self, session_id: UUID, result: CommandResult) -> None:
        """The mower refused a frame of ``session_id``: end that session (called by the hub)."""
        drive = self.drive
        if drive is None or drive.session_id != session_id:
            return
        busy = result.reason is not None and _BUSY_HINT in result.reason
        reason = DriveEndReason.MOWER_BUSY if busy else DriveEndReason.MOWER_REFUSED
        await self.end_drive(drive, reason, detail=result.reason)

    async def end_drive(
        self,
        drive: ActiveDrive,
        reason: DriveEndReason,
        *,
        detail: str | None = None,
        notify: bool = True,
    ) -> None:
        """End ``drive``: a zero frame, ``drive_ended`` (with ``notify``), ``fleet.drive.stop``.

        The zero frame makes the mower stop at once; without it, the dead-man timer of the last
        frame stops it within ``ttl_ms``. It is only sent while the last frame may still be
        moving the mower, so it never reopens a drive session the dead-man timer already ended.
        """
        if self.drive is not drive:
            return
        self.drive = None
        self.hub.unregister_drive(drive.session_id)
        if drive.timer is not None:
            drive.timer.cancel()
            drive.timer = None
        backend = self.hub.drive
        ttl = self.config.drive_ttl_ms / 1000
        live = (
            drive.last_frame_at is not None
            and asyncio.get_running_loop().time() - drive.last_frame_at < ttl
        )
        if backend is not None and self.principal is not None and live:
            stop = frame_envelope(
                drive,
                self.principal.ref(),
                linear=0.0,
                angular=0.0,
                blade_on=False,
                ttl_ms=self.config.drive_ttl_ms,
                now=self.clock(),
            )
            try:
                await backend.publish(stop)
            except Exception as exc:
                _log.warning("stop frame not published", error=repr(exc), session=str(self.id))
        if notify and not self.outbox.closing:
            self.send(
                DriveEndedMessage(
                    session_id=drive.session_id,
                    mower_id=drive.mower_id,
                    reason=reason,
                    detail=detail[:500] if detail else None,
                )
            )
        _log.info(
            "drive session ended",
            session=str(self.id),
            drive_session=str(drive.session_id),
            reason=reason.value,
            frames=drive.frames,
            dropped=drive.dropped,
        )
        if backend is not None:
            await backend.stop(
                DriveStopRequest(
                    session_id=drive.session_id,
                    account_id=drive.account_id,
                    mower_id=drive.mower_id,
                    reason=reason,
                    ended_at=self.clock(),
                    frames=drive.frames,
                    frames_dropped=drive.dropped,
                )
            )
