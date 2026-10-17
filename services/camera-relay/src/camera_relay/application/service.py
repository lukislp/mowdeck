"""Use cases of the relay: attach, renew, detach, media reports, source reports, mower states
and the periodic tick.

Every use case locks the key, feeds the input to the key's
:class:`~camera_relay.domain.session.RelaySession` with the clock's time and carries out the
returned effects in order. Requests name an account and a mower; a lease is only ever found in
the session of exactly that ``(account_id, mower_id)``:

* renew of an unknown lease is ``not_found``, of a lease of another account ``forbidden``, of a
  lease of another mower of the same account ``not_found``;
* detach is idempotent: an unknown lease (or one of another mower) is ``detached: false``
  without an error; a lease of another account is ``forbidden``.

Strict leases: a lease belongs to the principal that opened it (``requested_by`` of the attach,
compared by kind and subject). Only that principal renews or detaches it; another principal,
even a user of the same account, gets ``forbidden`` and the lease stays as it is. A Vision lease
is opened by Vision's service principal on behalf of a user, so it is renewed and detached by
that service principal only, never by the user's browser (and the other way round).
"""

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from mowdeck_contracts.v1 import EventEnvelope
from mowdeck_contracts.v1.camera import (
    RELAY_HEARTBEAT_SECONDS,
    CameraProvider,
    CameraSession,
    RelayEndpoint,
    RelayTile,
)
from mowdeck_contracts.v1.principal import PrincipalRef
from mowdeck_contracts.v1.relay import (
    RelayAttachReply,
    RelayAttachRequest,
    RelayDetachReply,
    RelayDetachRequest,
    RelayError,
    RelayErrorCode,
    RelayRenewReply,
    RelayRenewRequest,
)
from mowdeck_contracts.v1.subjects import camera_relay_event_subject
from mowdeck_service_kit import get_logger

from camera_relay.application.ports import (
    Clock,
    EventPublisher,
    MediaPlane,
    RelayRepository,
    SourceError,
    TicketSigner,
    VendorSessions,
)
from camera_relay.domain.effects import (
    Effect,
    EmitEvent,
    IssueTicket,
    KickViewer,
    StartVendorSession,
    StopVendorSession,
    UpdateCameras,
)
from camera_relay.domain.models import Lease, RelayKey, SessionState, VisionMode
from camera_relay.domain.policy import MowerStatus
from camera_relay.domain.session import RelaySession
from camera_relay.domain.tickets import TicketClaims, camera_path

_log = get_logger("camera_relay.service")

Step = Callable[[RelaySession, datetime], list[Effect]]
"""One input to a session at the given time, returning its effects."""

PRODUCER = "camera-relay"


@dataclass(frozen=True, slots=True)
class RelayConfig:
    """Settings of the use cases."""

    whep_base_url: str = "/media/whep"
    """Prefix of the WHEP URLs: ``<prefix>/r/<relay_id>/<camera>/whep``."""
    heartbeat_seconds: int = RELAY_HEARTBEAT_SECONDS


@dataclass(slots=True)
class _Outcome:
    ticket: str | None = None
    claims: TicketClaims | None = None
    start_error: RelayErrorCode | None = None
    start_detail: str = "the camera source did not start"


def _error(code: RelayErrorCode, detail: str) -> RelayError:
    return RelayError(code=code, detail=detail)


class RelayService:
    """The relay's use cases over a repository of sessions."""

    def __init__(
        self,
        *,
        repository: RelayRepository,
        vendor: VendorSessions,
        media: MediaPlane,
        events: EventPublisher,
        signer: TicketSigner,
        clock: Clock,
        config: RelayConfig | None = None,
    ) -> None:
        self.repository = repository
        self.vendor = vendor
        self.media = media
        self.events = events
        self.signer = signer
        self.clock = clock
        self.config = config or RelayConfig()
        self._locks: dict[RelayKey, asyncio.Lock] = {}

    def _lock(self, key: RelayKey) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    # --- Requests (relay.camera.*) ------------------------------------------------------------

    async def attach(
        self, request: RelayAttachRequest, *, vision_mode: VisionMode = VisionMode.STREAM
    ) -> RelayAttachReply:
        """Open a lease on the session of ``(account_id, mower_id)``.

        The first lease of an idle session asks for the vendor session first (``offer``): a
        provider without a source driver is answered with ``unsupported_provider`` before any
        lease, event or start exists, and the vendor session is not released (a client in
        ``auto`` falls back to the direct path and uses the stream). An empty ``camera_ids``
        means every camera the vendor session offers.
        """
        key = RelayKey(request.account_id, request.mower_id)
        async with self._lock(key):
            session = self.repository.get_or_create(key)
            now = self.clock()
            await self._execute(session, session.tick(now))
            offered = False
            if (
                session.state is SessionState.IDLE
                and session.admission(request.purpose, now) is None
            ):
                try:
                    offer = await self.vendor.offer(key)
                except SourceError as exc:
                    _log.info(
                        "camera source not offered",
                        mower_id=str(key.mower_id),
                        code=exc.code.value,
                        reason=str(exc),
                    )
                    detail = (str(exc) or "the camera source is not available")[:500]
                    return RelayAttachReply(error=_error(exc.code, detail))
                session.offer(offer.cameras)
                offered = True
            decision = session.attach(
                camera_ids=request.camera_ids,
                purpose=request.purpose,
                principal=request.requested_by,
                now=self.clock(),
                vision_mode=vision_mode,
            )
            outcome = await self._execute(session, decision.effects)
            if decision.refusal is not None:
                if offered:  # the offered vendor session starts nothing: let the mower stop
                    await self.vendor.discard(key)
                return RelayAttachReply(
                    error=_error(decision.refusal.code, decision.refusal.detail)
                )
            lease = decision.lease
            assert lease is not None  # noqa: S101 - a decision without refusal has a lease
            if lease.lease_id not in session.leases:
                code = outcome.start_error or RelayErrorCode.UNAVAILABLE
                return RelayAttachReply(error=_error(code, outcome.start_detail))
            if decision.budget_warning:
                _log.warning(
                    "viewer admitted below a vision limit",
                    mower_id=str(key.mower_id),
                    lease_id=str(lease.lease_id),
                )
            return RelayAttachReply(session=self._camera_session(session, lease, outcome))

    async def renew(self, request: RelayRenewRequest) -> RelayRenewReply:
        """Heartbeat of a lease: extend it and issue a fresh ticket (its own principal only)."""
        found = self._own_key(request.account_id, request.mower_id, request.lease_id)
        if isinstance(found, RelayError):
            return RelayRenewReply(error=found)
        async with self._lock(found):
            session = self.repository.get(found)
            if session is None:
                return RelayRenewReply(error=_not_found())
            if not _owned_by(session, request.lease_id, request.requested_by):
                return RelayRenewReply(error=_foreign_principal())
            decision = session.renew(request.lease_id, self.clock())
            outcome = await self._execute(session, decision.effects)
            if decision.lease is None:
                return RelayRenewReply(error=_not_found())
            return RelayRenewReply(session=self._camera_session(session, decision.lease, outcome))

    async def detach(self, request: RelayDetachRequest) -> RelayDetachReply:
        """End a lease. Idempotent for the lease's own principal; a lease of another account or
        of another principal is ``forbidden`` and stays open."""
        found = self._own_key(request.account_id, request.mower_id, request.lease_id)
        if isinstance(found, RelayError):
            if found.code is RelayErrorCode.FORBIDDEN:
                return RelayDetachReply(detached=False, error=found)
            return RelayDetachReply(detached=False)
        async with self._lock(found):
            session = self.repository.get(found)
            if session is None:
                return RelayDetachReply(detached=False)
            if not _owned_by(session, request.lease_id, request.requested_by):
                return RelayDetachReply(detached=False, error=_foreign_principal())
            decision = session.detach(request.lease_id, self.clock())
            await self._execute(session, decision.effects)
            return RelayDetachReply(detached=decision.lease is not None)

    def _own_key(self, account_id: UUID, mower_id: UUID, lease_id: UUID) -> RelayKey | RelayError:
        key = self.repository.key_of_lease(lease_id)
        if key is None:
            return _not_found()
        if key.account_id != account_id:
            return _error(RelayErrorCode.FORBIDDEN, "the lease belongs to another account")
        if key.mower_id != mower_id:
            return _not_found()
        return key

    # --- Reports of the media plane and the source --------------------------------------------

    async def media_confirmed(self, lease_id: UUID, whep_session: str) -> bool:
        """The reader ``whep_session`` of a viewer receives media. True when the lease exists."""
        return await self._on_lease(
            lease_id, lambda s, now: s.media_confirmed(lease_id, whep_session, now).effects
        )

    async def media_lost(self, lease_id: UUID, whep_session: str | None = None) -> bool:
        """The reader ``whep_session`` (any reader without it) of a viewer ended or stopped
        receiving media."""
        return await self._on_lease(
            lease_id, lambda s, now: s.media_lost(lease_id, now, whep_session).effects
        )

    async def source_started(
        self, key: RelayKey, session_id: UUID, source: CameraProvider, publishing: list[str]
    ) -> None:
        """The source of ``key`` has media."""
        await self._on_key(
            key, lambda s, now: s.source_started(session_id, source, publishing, now)
        )

    async def source_failed(self, key: RelayKey, session_id: UUID) -> None:
        """The source of ``key`` failed."""
        await self._on_key(key, lambda s, now: s.source_failed(session_id, now))

    async def mower_status_changed(self, mower_id: UUID, status: MowerStatus) -> None:
        """New state of a mower: applies to the session of every account that uses it."""
        self.repository.set_status(mower_id, status)
        for key in list(self.repository.keys_of_mower(mower_id)):
            await self._on_key(key, lambda s, now: s.mower_status_changed(status, now))

    async def tick(self) -> None:
        """Process the deadlines of every session and forget dormant ones."""
        for key in self.repository.session_keys():
            await self._on_key(key, lambda s, now: s.tick(now))
            lock = self._lock(key)
            session = self.repository.get(key)
            if session is not None and session.is_dormant(self.clock()) and not lock.locked():
                self.repository.remove(key)
                self._locks.pop(key, None)

    async def shutdown(self) -> None:
        """End every running session (reason ``shutdown``)."""
        for key in self.repository.session_keys():
            await self._on_key(key, lambda s, now: s.shutdown(now))

    async def _on_lease(self, lease_id: UUID, step: Step) -> bool:
        key = self.repository.key_of_lease(lease_id)
        if key is None:
            return False
        await self._on_key(key, step)
        return True

    async def _on_key(self, key: RelayKey, step: Step) -> None:
        async with self._lock(key):
            session = self.repository.get(key)
            if session is not None:
                await self._execute(session, step(session, self.clock()))

    # --- Effects ------------------------------------------------------------------------------

    async def _execute(self, session: RelaySession, effects: list[Effect]) -> _Outcome:
        """Carry out the effects in order; a failed start feeds ``source_failed`` back."""
        outcome = _Outcome()
        queue: deque[Effect] = deque(effects)
        while queue:
            effect = queue.popleft()
            match effect:
                case StartVendorSession():
                    try:
                        await self.vendor.start(effect)
                    except SourceError as exc:
                        _log.info(
                            "camera source did not start",
                            mower_id=str(effect.key.mower_id),
                            code=exc.code.value,
                            reason=str(exc),
                        )
                        outcome.start_error = exc.code
                        outcome.start_detail = (str(exc) or outcome.start_detail)[:500]
                        # No driver for the provider: leave the vendor session to the direct
                        # path instead of stopping the mower's stream.
                        release = exc.code is not RelayErrorCode.UNSUPPORTED_PROVIDER
                        queue.extend(
                            session.source_failed(effect.session_id, self.clock(), release=release)
                        )
                case UpdateCameras():
                    try:
                        await self.vendor.update_cameras(effect)
                    except SourceError:
                        _log.warning("camera set not updated", mower_id=str(effect.key.mower_id))
                case StopVendorSession():
                    try:
                        await self.vendor.stop(effect)
                    except Exception:
                        _log.exception("camera source not stopped cleanly")
                    queue.extend(session.stop_completed(effect.session_id, self.clock()))
                case IssueTicket():
                    outcome.ticket = self.signer.sign(effect.claims)
                    outcome.claims = effect.claims
                case KickViewer():
                    try:
                        await self.media.kick(effect.whep_session)
                    except Exception:
                        _log.exception("viewer not kicked", lease_id=str(effect.lease_id))
                case EmitEvent():
                    await self._publish(effect)
        return outcome

    async def _publish(self, effect: EmitEvent) -> None:
        envelope: EventEnvelope[Any] = EventEnvelope(
            type=effect.event_type,
            occurred_at=effect.occurred_at,
            account_id=effect.key.account_id,
            producer=PRODUCER,
            payload=effect.payload,
        )
        try:
            await self.events.publish(
                camera_relay_event_subject(effect.key.mower_id, effect.kind), envelope
            )
        except Exception:
            _log.exception("relay event not published", type=effect.event_type)

    def _camera_session(
        self, session: RelaySession, lease: Lease, outcome: _Outcome
    ) -> CameraSession:
        assert outcome.ticket is not None and outcome.claims is not None  # noqa: S101
        assert session.relay_id is not None  # noqa: S101
        base = self.config.whep_base_url.rstrip("/")
        tiles = [
            RelayTile(
                camera_id=camera, whep_url=f"{base}/{camera_path(session.relay_id, camera)}/whep"
            )
            for camera in lease.camera_ids
        ]
        return CameraSession(
            provider=CameraProvider.RELAY,
            app_id="",
            channel="",
            token="",
            uid=0,
            cameras=session.camera_infos(lease.camera_ids),
            expires_at=outcome.claims.expires_at,
            relay=RelayEndpoint(
                tiles=tiles,
                ticket=outcome.ticket,
                lease_id=lease.lease_id,
                heartbeat_seconds=self.config.heartbeat_seconds,
            ),
        )


def _not_found() -> RelayError:
    return _error(RelayErrorCode.NOT_FOUND, "no such lease (expired or detached)")


def _same_principal(a: PrincipalRef, b: PrincipalRef) -> bool:
    """The same person or service: kind and subject (the token id changes with every login)."""
    return a.kind is b.kind and a.subject == b.subject


def _owned_by(session: RelaySession, lease_id: UUID, principal: PrincipalRef) -> bool:
    """The lease is unknown here (the session decides) or belongs to ``principal``."""
    lease = session.leases.get(lease_id)
    return lease is None or _same_principal(lease.principal, principal)


def _foreign_principal() -> RelayError:
    return _error(RelayErrorCode.FORBIDDEN, "the lease belongs to another principal")
