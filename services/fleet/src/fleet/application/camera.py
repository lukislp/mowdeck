"""Live camera sessions (ADR 0032): Fleet relays the request to the Device-Gateway and audits it.

The HTTP layer checks ``can_view`` on the mower (404 semantics). Here: the plan feature
``camera``, the mower in the caller's account, one request to the gateway that holds the mower
(``gateway.mower.<id>.camera.session``) and an audit row ``mower.camera_opened`` (who opened
the camera when, and the provider). The session itself (with its short-lived token) goes back
to the caller and is never stored or logged.

Why ``can_view`` and not ``can_control``: watching changes nothing on the mower, and the people
who may see the mower's position and map may see its surroundings too. Owners who want to
restrict the camera give those users no access to the mower or switch off the plan feature.
"""

import logging
from uuid import UUID

from mowdeck_contracts.v1 import (
    CAMERA_FEATURE,
    CameraSession,
    CameraSessionErrorCode,
    CameraSessionRequest,
    PrincipalRef,
)
from mowdeck_service_kit.entitlements import Entitlements

from fleet.application.context import Actor
from fleet.application.plan import allow_all, require_feature
from fleet.application.ports import CameraGateway, Clock, Store
from fleet.domain.errors import CameraUnsupportedError, NotFoundError, Unavailable
from fleet.domain.models import AuditEntry

_log = logging.getLogger("fleet.camera")

CAMERA_OPENED = "mower.camera_opened"
"""Audit action of an issued camera session."""


class CameraService:
    """Use case: open a live camera session for one mower."""

    def __init__(
        self,
        store: Store,
        gateway: CameraGateway,
        clock: Clock,
        entitlements: Entitlements | None = None,
    ) -> None:
        self.store = store
        self.gateway = gateway
        self.clock = clock
        self.entitlements = entitlements if entitlements is not None else allow_all()

    async def open(self, actor: Actor, account_id: UUID, mower_id: UUID) -> CameraSession:
        """A camera session for the mower (the caller holds ``can_view``).

        Raises :class:`FeatureNotInPlanError` (403), :class:`NotFoundError` (404),
        :class:`CameraUnsupportedError` (409) or :class:`Unavailable` (503).
        """
        await require_feature(self.entitlements, account_id, CAMERA_FEATURE)
        if actor.principal is None:
            raise NotFoundError("mower not found")
        async with self.store.transaction() as tx:
            target = await tx.mowers.get(id=mower_id, account_id=account_id)
        if target is None:
            raise NotFoundError("mower not found")
        principal = actor.principal
        request = CameraSessionRequest(
            mower_id=target.id,
            account_id=target.account_id,
            requested_by=PrincipalRef(
                subject=principal.subject,
                kind=principal.kind,
                display_name=principal.display_name,
                token_id=principal.token_id,
            ),
        )
        try:
            reply = await self.gateway.request(request)
        except Exception as exc:
            _log.warning(
                "camera session request failed",
                extra={"mower_id": str(mower_id), "error": type(exc).__name__},
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
        session = reply.session
        if session is None:  # pragma: no cover - the reply model guarantees one of both
            raise Unavailable("the device gateway sent no session")
        async with self.store.transaction() as tx:
            await tx.audit.add(
                AuditEntry(
                    at=self.clock(),
                    action=CAMERA_OPENED,
                    account_id=target.account_id,
                    target_type="mower",
                    target_id=str(target.id),
                    actor_subject=actor.subject,
                    actor_kind=principal.kind.value,
                    details={
                        "provider": session.provider.value,
                        "expires_at": session.expires_at.isoformat(),
                        "cameras": len(session.cameras),
                    },
                    correlation_id=actor.correlation_id,
                )
            )
        _log.info(
            "camera session opened",
            extra={"mower_id": str(mower_id), "provider": session.provider.value},
        )
        return session
