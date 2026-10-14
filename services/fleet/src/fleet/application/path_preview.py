"""Mow path previews: Fleet relays "plan this start" to the Device-Gateway of the mower.

The HTTP layer checks ``can_control`` on the mower (404 semantics): planning makes the mower
compute a route, which is part of starting it. Here: the plan feature ``control`` (like every
start), the mower in the caller's account, then one request to the gateway that holds the mower
(``gateway.mower.<id>.path.preview``). The mower plans without starting, so nothing is audited;
the route goes back to the caller and is not stored.
"""

import logging
from uuid import UUID

from mowdeck_contracts.v1 import (
    MowPath,
    PathPreviewErrorCode,
    PathPreviewGatewayRequest,
    PathPreviewRequest,
    PrincipalRef,
)
from mowdeck_service_kit.bus import RequestTimeoutError
from mowdeck_service_kit.entitlements import Entitlements

from fleet.application.context import Actor
from fleet.application.plan import FEATURE_CONTROL, allow_all, require_feature
from fleet.application.ports import PathPreviewGateway, Store
from fleet.domain.errors import (
    GatewayTimeout,
    NotFoundError,
    PathPreviewRejectedError,
    PathPreviewUnsupportedError,
    Unavailable,
)

_log = logging.getLogger("fleet.path_preview")


class PathPreviewService:
    """Use case: let a mower plan the route of a start without starting it."""

    def __init__(
        self,
        store: Store,
        gateway: PathPreviewGateway,
        entitlements: Entitlements | None = None,
    ) -> None:
        self.store = store
        self.gateway = gateway
        self.entitlements = entitlements if entitlements is not None else allow_all()

    async def preview(
        self, actor: Actor, account_id: UUID, mower_id: UUID, body: PathPreviewRequest
    ) -> MowPath:
        """The route the mower planned for ``body`` (the caller holds ``can_control``).

        Raises :class:`FeatureNotInPlanError` (403), :class:`NotFoundError` (404),
        :class:`PathPreviewUnsupportedError` or :class:`PathPreviewRejectedError` (409),
        :class:`Unavailable` (503) or :class:`GatewayTimeout` (504).
        """
        await require_feature(self.entitlements, account_id, FEATURE_CONTROL)
        if actor.principal is None:
            raise NotFoundError("mower not found")
        async with self.store.transaction() as tx:
            target = await tx.mowers.get(id=mower_id, account_id=account_id)
        if target is None:
            raise NotFoundError("mower not found")
        principal = actor.principal
        request = PathPreviewGatewayRequest(
            mower_id=target.id,
            account_id=target.account_id,
            area_ids=body.area_ids,
            pattern=body.pattern,
            requested_by=PrincipalRef(
                subject=principal.subject,
                kind=principal.kind,
                display_name=principal.display_name,
                token_id=principal.token_id,
            ),
        )
        try:
            reply = await self.gateway.request(request)
        except RequestTimeoutError as exc:
            raise GatewayTimeout("the mower did not hand back its route in time") from exc
        except Exception as exc:
            _log.warning(
                "path preview request failed",
                extra={"mower_id": str(mower_id), "error": type(exc).__name__},
            )
            raise Unavailable("the device gateway did not answer") from exc
        if reply.error is not None:
            detail = reply.error.detail
            match reply.error.code:
                case PathPreviewErrorCode.UNSUPPORTED:
                    raise PathPreviewUnsupportedError(detail)
                case PathPreviewErrorCode.REJECTED:
                    raise PathPreviewRejectedError(detail)
                case PathPreviewErrorCode.FORBIDDEN:
                    raise NotFoundError("mower not found")
                case PathPreviewErrorCode.TIMEOUT:
                    raise GatewayTimeout(detail or "the mower did not hand back its route in time")
                case PathPreviewErrorCode.NOT_FOUND:
                    raise Unavailable("the mower is not held by a device gateway right now")
                case _:
                    raise Unavailable(detail or "the mower planned no route")
        path = reply.path
        if path is None:  # pragma: no cover - the reply model guarantees one of both
            raise Unavailable("the device gateway sent no route")
        _log.info(
            "path preview",
            extra={"mower_id": str(mower_id), "points": path.points_total},
        )
        return path
