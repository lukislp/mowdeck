"""Live camera sessions: Fleet is the front door for the direct path (ADR 0032) and the camera
relay (ADR 0045), and audits both.

The HTTP layer checks ``can_view`` on the mower (404 semantics). Here: the plan feature
``camera``, the mower in the caller's account (always the token's account, never the client's),
then the transport:

* ``direct``: one request to the gateway that holds the mower
  (``gateway.mower.<id>.camera.session``); the browser joins the vendor channel itself.
* ``relay``: a lease on the camera relay (``relay.camera.attach``, every camera the mower
  offers); the relay's ``CameraSession`` (provider ``relay``) goes back unchanged. The relay's
  errors are answered, never a fallback; a relay that is not deployed or does not answer is 503.
* ``auto`` (the default): the relay, and the direct path **only** when the relay answers
  ``unsupported_provider`` (no source driver for the mower's provider) or is not deployed (no
  responder, no answer in time, no bus). ``forbidden``, ``not_found``, ``budget`` and
  ``unavailable`` are answered as they are.

Every issued session writes an audit row ``mower.camera_opened`` (who, when, the provider and
``transport``: the path actually used). The session itself (with its short-lived token or
ticket) goes back to the caller and is never stored or logged.

Renew and detach act on a relay lease of the mower in the caller's account and carry the
token's principal (``requested_by``). Leases are strict: the relay refuses a lease of another
account or one another user opened (``forbidden``), which Fleet answers like a hidden object
(404); that lease keeps running.

A still image (:meth:`CameraService.snapshot`, for API clients such as Home Assistant's camera
entity) is asked from the Device-Gateway on ``gateway.mower.<id>.photo``, the request Vision
uses, with the demand ``routine``: the gateway uses the mower's own photo function or the
camera relay's frame tap, which keeps every camera limit (battery, Vision budget, a sleeping
mower is not woken). Fleet takes at most one image per mower every
``snapshot_min_interval_seconds`` and hands the last one to every request in between, so a
dashboard that refreshes often costs the mower nothing more. A new image is audited as
``mower.camera_snapshot``; the image itself is never stored.

The relay's lifecycle events (stream ``CAMERA``) become the audit rows
``mower.camera_stream_started`` and ``mower.camera_stream_stopped`` of the event's account
(:meth:`CameraService.record_relay_event`), once per event id however often it is delivered.

Why ``can_view`` and not ``can_control``: watching changes nothing on the mower, and the people
who may see the mower's position and map may see its surroundings too. Owners who want to
restrict the camera give those users no access to the mower or switch off the plan feature.
"""

import asyncio
import base64
import binascii
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from mowdeck_contracts.v1 import (
    CAMERA_FEATURE,
    CameraSession,
    CameraSessionErrorCode,
    CameraSessionRequest,
    CameraTransport,
    EventEnvelope,
    PhotoErrorCode,
    PhotoRequest,
    PrincipalRef,
    VisionDemand,
)
from mowdeck_contracts.v1.relay import (
    CAMERA_RELAY_STARTED,
    CAMERA_RELAY_STOPPED,
    CameraRelayStarted,
    CameraRelayStopped,
    RelayAttachRequest,
    RelayDetachRequest,
    RelayError,
    RelayErrorCode,
    RelayPurpose,
    RelayRenewRequest,
)
from mowdeck_service_kit.bus import BusError, BusNotConnectedError, NoRespondersError
from mowdeck_service_kit.entitlements import Entitlements

from fleet.application.context import Actor
from fleet.application.plan import allow_all, require_feature
from fleet.application.ports import CameraGateway, CameraRelay, Clock, PhotoGateway, Store
from fleet.domain.errors import (
    CameraBudgetError,
    CameraRelayUnsupportedError,
    CameraUnsupportedError,
    ConflictError,
    FleetError,
    NotFoundError,
    Unavailable,
)
from fleet.domain.models import AuditEntry, Mower

_log = logging.getLogger("fleet.camera")

CAMERA_OPENED = "mower.camera_opened"
"""Audit action of an issued camera session (``details.transport``: ``direct`` or ``relay``)."""
CAMERA_SNAPSHOT = "mower.camera_snapshot"
"""Audit action of a new still image (``details.media_type``, ``bytes``; never the image)."""
CAMERA_STREAM_STARTED = "mower.camera_stream_started"
"""Audit action of a relay session that went live (event ``camera.relay.started``)."""
CAMERA_STREAM_STOPPED = "mower.camera_stream_stopped"
"""Audit action of a relay session that ended (event ``camera.relay.stopped``)."""

RELAY_SERVICE_ACTOR = "service:camera-relay"
"""Actor of a stream audit row that no person caused (the stop after the grace)."""


@dataclass(frozen=True, slots=True)
class Snapshot:
    """A still image of a mower's camera."""

    content: bytes
    media_type: str
    taken_at: datetime
    """When the gateway reported the image taken (else when Fleet received it)."""
    fetched_at: datetime
    """When Fleet received it; with ``snapshot_min_interval_seconds`` the age of the cache."""


class RelayNotAnswering(Exception):  # noqa: N818 - a condition, not a failure of Fleet
    """The camera relay is not deployed or did not answer (no responder, timeout, no bus)."""


def _ref(actor: Actor) -> PrincipalRef:
    principal = actor.principal
    assert principal is not None  # noqa: S101 - checked by the callers
    return PrincipalRef(
        subject=principal.subject,
        kind=principal.kind,
        display_name=principal.display_name,
        token_id=principal.token_id,
    )


class CameraService:
    """Use cases: open, renew and detach a live camera session; audit the relay's events."""

    def __init__(
        self,
        store: Store,
        gateway: CameraGateway,
        clock: Clock,
        entitlements: Entitlements | None = None,
        relay: CameraRelay | None = None,
        *,
        photos: PhotoGateway | None = None,
        snapshot_min_interval_seconds: float = 30.0,
    ) -> None:
        self.store = store
        self.gateway = gateway
        self.relay = relay
        self.photos = photos
        self.clock = clock
        self.entitlements = entitlements if entitlements is not None else allow_all()
        self.snapshot_min_interval = timedelta(seconds=snapshot_min_interval_seconds)
        self._snapshots: dict[UUID, Snapshot] = {}
        self._snapshot_locks: dict[UUID, asyncio.Lock] = {}

    def forget(self, mower_ids: Iterable[UUID]) -> int:
        """Drop the cached still images of these mowers (demo purge); returns how many."""
        dropped = 0
        for mower_id in mower_ids:
            self._snapshot_locks.pop(mower_id, None)
            if self._snapshots.pop(mower_id, None) is not None:
                dropped += 1
        return dropped

    async def _mower(self, actor: Actor, account_id: UUID, mower_id: UUID) -> Mower:
        if actor.principal is None:
            raise NotFoundError("mower not found")
        async with self.store.transaction() as tx:
            target = await tx.mowers.get(id=mower_id, account_id=account_id)
        if target is None:
            raise NotFoundError("mower not found")
        return target

    # --- Open ---------------------------------------------------------------------------------

    async def open(
        self,
        actor: Actor,
        account_id: UUID,
        mower_id: UUID,
        transport: CameraTransport = CameraTransport.AUTO,
    ) -> CameraSession:
        """A camera session for the mower (the caller holds ``can_view``).

        Raises :class:`FeatureNotInPlanError` (403), :class:`NotFoundError` (404),
        :class:`CameraUnsupportedError`, :class:`CameraRelayUnsupportedError`,
        :class:`CameraBudgetError` (409) or :class:`Unavailable` (503).
        """
        await require_feature(self.entitlements, account_id, CAMERA_FEATURE)
        target = await self._mower(actor, account_id, mower_id)
        used = CameraTransport.DIRECT
        session: CameraSession | None = None
        if transport is not CameraTransport.DIRECT:
            try:
                session = await self._attach(actor, target)
                used = CameraTransport.RELAY
            except RelayNotAnswering as exc:
                if transport is CameraTransport.RELAY:
                    msg = "the camera relay is not deployed or did not answer"
                    raise Unavailable(msg) from exc
                _log.info(
                    "camera relay not answering, direct path",
                    extra={"mower_id": str(mower_id), "reason": str(exc)},
                )
            except CameraRelayUnsupportedError:
                if transport is CameraTransport.RELAY:
                    raise
                _log.info(
                    "camera relay cannot serve the provider, direct path",
                    extra={"mower_id": str(mower_id)},
                )
        if session is None:
            session = await self._direct(actor, target)
        details: dict[str, Any] = {
            "provider": session.provider.value,
            "transport": used.value,
            "requested_transport": transport.value,
            "expires_at": session.expires_at.isoformat(),
            "cameras": len(session.relay.tiles) if session.relay else len(session.cameras),
        }
        if session.relay is not None:
            details["lease_id"] = str(session.relay.lease_id)
        await self._audit(actor, target, CAMERA_OPENED, details)
        _log.info(
            "camera session opened",
            extra={
                "mower_id": str(mower_id),
                "provider": session.provider.value,
                "transport": used.value,
            },
        )
        return session

    async def _direct(self, actor: Actor, target: Mower) -> CameraSession:
        request = CameraSessionRequest(
            mower_id=target.id, account_id=target.account_id, requested_by=_ref(actor)
        )
        try:
            reply = await self.gateway.request(request)
        except Exception as exc:
            _log.warning(
                "camera session request failed",
                extra={"mower_id": str(target.id), "error": type(exc).__name__},
            )
            raise Unavailable("the device gateway did not answer") from exc
        if reply.error is not None:
            match reply.error.code:
                case CameraSessionErrorCode.UNSUPPORTED:
                    raise CameraUnsupportedError(reply.error.detail)
                case CameraSessionErrorCode.FORBIDDEN:
                    raise NotFoundError("mower not found")
                case CameraSessionErrorCode.NOT_FOUND:
                    raise Unavailable("the mower is not held by a device gateway right now")
                case _:
                    raise Unavailable(reply.error.detail or "the camera is not available")
        if reply.session is None:  # pragma: no cover - the reply model guarantees one of both
            raise Unavailable("the device gateway sent no session")
        return reply.session

    async def _attach(self, actor: Actor, target: Mower) -> CameraSession:
        request = RelayAttachRequest(
            account_id=target.account_id,
            mower_id=target.id,
            camera_ids=[],  # every camera the mower offers
            requested_by=_ref(actor),
            purpose=RelayPurpose.VIEWER,
        )
        relay = self._relay()
        try:
            reply = await relay.attach(request)
        except BusError as exc:
            raise RelayNotAnswering(type(exc).__name__) from exc
        except Exception as exc:
            _log.warning(
                "camera relay attach failed",
                extra={"mower_id": str(target.id), "error": type(exc).__name__},
            )
            raise Unavailable("the camera relay sent no valid answer") from exc
        if reply.error is not None:
            raise _relay_error(reply.error, lease=False)
        if reply.session is None:  # pragma: no cover - the reply model guarantees one of both
            raise Unavailable("the camera relay sent no session")
        return reply.session

    def _relay(self) -> CameraRelay:
        if self.relay is None:
            msg = "no camera relay port"
            raise RelayNotAnswering(msg)
        return self.relay

    # --- Still image --------------------------------------------------------------------------

    async def snapshot(self, actor: Actor, account_id: UUID, mower_id: UUID) -> Snapshot:
        """A still image of the mower's camera (the caller holds ``can_view``): the last one
        while it is younger than ``snapshot_min_interval_seconds``, else a new one. Concurrent
        requests for the same mower share one photo request.

        Raises :class:`FeatureNotInPlanError` (403), :class:`NotFoundError` (404),
        :class:`CameraUnsupportedError` (409) or :class:`Unavailable` (503).
        """
        await require_feature(self.entitlements, account_id, CAMERA_FEATURE)
        target = await self._mower(actor, account_id, mower_id)
        lock = self._snapshot_locks.setdefault(target.id, asyncio.Lock())
        async with lock:
            cached = self._snapshots.get(target.id)
            if cached is not None and self.clock() - cached.fetched_at < self.snapshot_min_interval:
                return cached
            snapshot = await self._take_photo(target)
            self._snapshots[target.id] = snapshot
        await self._audit(
            actor,
            target,
            CAMERA_SNAPSHOT,
            {"media_type": snapshot.media_type, "bytes": len(snapshot.content)},
        )
        return snapshot

    async def _take_photo(self, target: Mower) -> Snapshot:
        if self.photos is None:
            raise Unavailable("no device gateway is reachable")
        request = PhotoRequest(
            mower_id=target.id, account_id=target.account_id, demand=VisionDemand.ROUTINE
        )
        try:
            reply = await self.photos.request(request)
        except Exception as exc:
            _log.warning(
                "camera snapshot request failed",
                extra={"mower_id": str(target.id), "error": type(exc).__name__},
            )
            raise Unavailable("the device gateway did not answer") from exc
        if reply.error is not None:
            match reply.error.code:
                case PhotoErrorCode.UNSUPPORTED:
                    raise CameraUnsupportedError(reply.error.detail)
                case PhotoErrorCode.NOT_FOUND:
                    raise Unavailable("the mower is not held by a device gateway right now")
                case _:
                    raise Unavailable(reply.error.detail or "the camera is not available")
        encoded: str | None = reply.image_base64
        media_type: str | None = reply.media_type
        if reply.frames:
            encoded, media_type = reply.frames[0].image_base64, reply.frames[0].media_type
        if encoded is None or media_type is None:  # pragma: no cover - the model ensures both
            raise Unavailable("the device gateway sent no image")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise Unavailable("the device gateway sent an unreadable image") from exc
        now = self.clock()
        return Snapshot(content, media_type, reply.taken_at or now, now)

    # --- Renew and detach ---------------------------------------------------------------------

    async def renew(
        self, actor: Actor, account_id: UUID, mower_id: UUID, lease_id: UUID
    ) -> CameraSession:
        """The heartbeat of a relay lease: the same lease with a fresh ticket.

        Raises :class:`FeatureNotInPlanError` (403), :class:`NotFoundError` (404: unknown or
        expired lease, a lease of another mower, account or user) or :class:`Unavailable` (503).
        """
        await require_feature(self.entitlements, account_id, CAMERA_FEATURE)
        target = await self._mower(actor, account_id, mower_id)
        request = RelayRenewRequest(
            account_id=target.account_id,
            mower_id=target.id,
            lease_id=lease_id,
            requested_by=_ref(actor),
        )
        try:
            reply = await self._relay().renew(request)
        except RelayNotAnswering as exc:
            raise Unavailable("the camera relay is not deployed") from exc
        except BusError as exc:
            raise Unavailable("the camera relay is not deployed or did not answer") from exc
        except Exception as exc:
            raise Unavailable("the camera relay sent no valid answer") from exc
        if reply.error is not None:
            raise _relay_error(reply.error, lease=True)
        if reply.session is None:  # pragma: no cover - the reply model guarantees one of both
            raise Unavailable("the camera relay sent no session")
        return reply.session

    async def detach(self, actor: Actor, account_id: UUID, mower_id: UUID, lease_id: UUID) -> bool:
        """End a relay lease. Idempotent: an unknown or closed lease (or no relay at all)
        returns False. No plan check: ending a stream is always allowed.

        Raises :class:`NotFoundError` (404: the mower, or a lease of another account or user) or
        :class:`Unavailable` (503: the relay did not answer in time).
        """
        target = await self._mower(actor, account_id, mower_id)
        request = RelayDetachRequest(
            account_id=target.account_id,
            mower_id=target.id,
            lease_id=lease_id,
            requested_by=_ref(actor),
        )
        try:
            reply = await self._relay().detach(request)
        except RelayNotAnswering:
            return False
        except (NoRespondersError, BusNotConnectedError):
            return False  # no relay deployed: no lease to end
        except BusError as exc:
            raise Unavailable("the camera relay did not answer") from exc
        except Exception as exc:
            raise Unavailable("the camera relay sent no valid answer") from exc
        if reply.error is not None:
            raise _relay_error(reply.error, lease=True)
        return reply.detached

    # --- Audit of the relay's events ----------------------------------------------------------

    async def record_relay_event(self, envelope: EventEnvelope[dict[str, Any]]) -> bool:
        """Write the audit row of a ``camera.relay.started`` or ``camera.relay.stopped`` event.

        Idempotent per event id (a redelivery writes nothing); other relay events are ignored.
        Returns True when a row was written. Raises ``ValueError`` for an invalid payload.
        """
        if envelope.type == CAMERA_RELAY_STARTED:
            started = CameraRelayStarted.model_validate(envelope.payload)
            return await self._stream_row(
                envelope,
                started.account_id,
                started.mower_id,
                CAMERA_STREAM_STARTED,
                {
                    "relay_session_id": str(started.relay_session_id),
                    "source": started.source.value,
                    "cameras": list(started.camera_ids),
                    "trigger": started.trigger.value if started.trigger else None,
                    "started_at": started.started_at.isoformat(),
                },
                actor=started.started_by,
                at=started.started_at,
            )
        if envelope.type == CAMERA_RELAY_STOPPED:
            stopped = CameraRelayStopped.model_validate(envelope.payload)
            duration = (
                round((stopped.stopped_at - stopped.started_at).total_seconds(), 3)
                if stopped.started_at is not None
                else None
            )
            return await self._stream_row(
                envelope,
                stopped.account_id,
                stopped.mower_id,
                CAMERA_STREAM_STOPPED,
                {
                    "relay_session_id": str(stopped.relay_session_id),
                    "reason": stopped.reason.value,
                    "trigger": stopped.trigger.value if stopped.trigger else None,
                    "started_at": stopped.started_at.isoformat() if stopped.started_at else None,
                    "stopped_at": stopped.stopped_at.isoformat(),
                    "duration_seconds": duration,
                    "peak_viewers": stopped.peak_viewers,
                    "vision_seconds": stopped.vision_seconds,
                },
                actor=None,
                at=stopped.stopped_at,
            )
        return False

    async def _stream_row(
        self,
        envelope: EventEnvelope[dict[str, Any]],
        account_id: UUID,
        mower_id: UUID,
        action: str,
        details: dict[str, Any],
        *,
        actor: PrincipalRef | None,
        at: datetime,
    ) -> bool:
        if envelope.account_id is not None and envelope.account_id != account_id:
            msg = "the envelope's account differs from the payload's"
            raise ValueError(msg)
        entry = AuditEntry(
            at=at,
            action=action,
            account_id=account_id,
            target_type="mower",
            target_id=str(mower_id),
            actor_subject=(
                f"{actor.kind.value}:{actor.subject}" if actor is not None else RELAY_SERVICE_ACTOR
            ),
            actor_kind=actor.kind.value if actor is not None else "service",
            details=details,
            correlation_id=envelope.correlation_id,
            event_id=envelope.id,
        )
        try:
            async with self.store.transaction() as tx:
                if await tx.audit.count(event_id=envelope.id):
                    return False
                await tx.audit.add(entry)
        except ConflictError:
            return False  # written by a concurrent delivery of the same event
        return True

    async def _audit(
        self, actor: Actor, target: Mower, action: str, details: dict[str, Any]
    ) -> None:
        principal = actor.principal
        assert principal is not None  # noqa: S101 - checked in _mower
        async with self.store.transaction() as tx:
            await tx.audit.add(
                AuditEntry(
                    at=self.clock(),
                    action=action,
                    account_id=target.account_id,
                    target_type="mower",
                    target_id=str(target.id),
                    actor_subject=actor.subject,
                    actor_kind=principal.kind.value,
                    details=details,
                    correlation_id=actor.correlation_id,
                )
            )


def _relay_error(error: RelayError, *, lease: bool) -> FleetError:
    """The use case error of a relay error code (consistent with the direct path: a foreign
    object is hidden, a missing source is 503)."""
    hidden = "camera session not found" if lease else "mower not found"
    match error.code:
        case RelayErrorCode.UNSUPPORTED_PROVIDER:
            return CameraRelayUnsupportedError(error.detail)
        case RelayErrorCode.FORBIDDEN | RelayErrorCode.NOT_FOUND:
            return NotFoundError(hidden)
        case RelayErrorCode.BUDGET:
            return CameraBudgetError(error.detail)
        case _:
            return Unavailable(error.detail or "the camera relay is not available")
