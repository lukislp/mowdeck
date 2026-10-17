"""Internal HTTP routes of the relay (never routed through Traefik).

* ``POST /internal/mediamtx/auth``: MediaMTX's HTTP auth callback (``authHTTPAddress``). Answers
  ``200`` (allowed) or ``401`` (denied) with an empty body; the decisions are in
  :mod:`camera_relay.application.media_auth`. The body is MediaMTX's (``user``, ``password``,
  ``token``, ``ip``, ``action``, ``path``, ``protocol``, ``id``, ``query``); unknown fields are
  ignored. A ticket or password is never logged.
* ``POST /internal/v1/test/freeze``: only with ``MOWDECK_RELAY_TEST_HOOKS=true`` (the test
  stacks). Body ``{"frozen": true|false, "relay_id": optional}``: pauses or resumes the fake
  sources, so a test sees a frozen picture (no new frames) and its recovery.
"""

from typing import Protocol

from fastapi import APIRouter, HTTPException, Response
from mowdeck_service_kit import get_logger
from pydantic import BaseModel, ConfigDict, Field

from camera_relay.application.media_auth import MediaAuthorizer, MediaAuthRequest

_log = get_logger("camera_relay.http")


class MediaMtxAuthBody(BaseModel):
    """The callback body of MediaMTX."""

    model_config = ConfigDict(extra="ignore")

    user: str = Field(default="", max_length=256)
    password: str = Field(default="", max_length=4096, repr=False)
    token: str = Field(default="", max_length=4096, repr=False)
    ip: str = Field(default="", max_length=64)
    action: str = Field(default="", max_length=32)
    path: str = Field(default="", max_length=512)
    protocol: str = Field(default="", max_length=32)
    id: str | None = Field(default=None, max_length=64)


def auth_router(authorizer: MediaAuthorizer) -> APIRouter:
    """The auth callback of the media server."""
    router = APIRouter()

    @router.post("/internal/mediamtx/auth", include_in_schema=False)
    async def mediamtx_auth(body: MediaMtxAuthBody) -> Response:
        decision = authorizer.decide(
            MediaAuthRequest(
                action=body.action,
                path=body.path,
                protocol=body.protocol,
                user=body.user,
                password=body.password,
                token=body.token,
                session_id=body.id,
            )
        )
        # Internal requests (the API every 2 s, publishers, RTSP clients that first ask without
        # credentials) are debug; viewer reads and other denials are info.
        internal = body.protocol != "webrtc" and (decision.allowed or not body.user)
        log = _log.debug if internal else _log.info
        log(
            "media server auth",
            action=body.action,
            protocol=body.protocol,
            path=body.path,
            ip=body.ip,
            session=body.id,
            allowed=decision.allowed,
            reason=decision.reason,
            lease_id=str(decision.lease_id) if decision.lease_id else None,
        )
        return Response(status_code=200 if decision.allowed else 401)

    return router


class Freezable(Protocol):
    """A source that can be frozen (the fake source driver)."""

    def freeze(self, frozen: bool, relay_id: str | None = None) -> int:
        """Pause or resume; returns how many processes changed."""
        ...


class FreezeBody(BaseModel):
    """Body of the freeze hook."""

    model_config = ConfigDict(extra="forbid")

    frozen: bool = True
    relay_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")


class FreezeReply(BaseModel):
    """What changed."""

    frozen: bool
    changed: int


def hooks_router(source: Freezable) -> APIRouter:
    """Test-only hooks (``MOWDECK_RELAY_TEST_HOOKS=true``)."""
    router = APIRouter()

    @router.post("/internal/v1/test/freeze", include_in_schema=False)
    async def freeze(body: FreezeBody) -> FreezeReply:
        try:
            changed = source.freeze(body.frozen, body.relay_id)
        except NotImplementedError as exc:
            raise HTTPException(status_code=501, detail=str(exc)) from exc
        _log.warning("test hook: fake source freeze", frozen=body.frozen, changed=changed)
        return FreezeReply(frozen=body.frozen, changed=changed)

    return router
