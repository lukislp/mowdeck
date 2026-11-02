"""Routes of the theft tracking mode under ``/api/v1/telemetry`` (ADR 0058).

Reading a session, its links and the report need ``can_view`` on the mower; starting, ending,
photos, links and deleting need ``can_control`` (404 for foreign mowers, 403 when visible
without it). ``POST /public/tracking/view`` needs no login: the share token is the credential;
it travels in the body (never in a URL, so no access log, referrer or history holds it) and the
answer carries only the allowed fields.
"""

import dataclasses
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Request, Response
from mowdeck_service_kit import CurrentAccountId
from mowdeck_service_kit.client_address import client_address, limiter_key
from mowdeck_service_kit.errors import NotFound
from pydantic import BaseModel, Field

from telemetry.adapters.api.deps import ControlMower, ServicesDep, SharedReads, ViewMower
from telemetry.adapters.api.schemas import AerialBounds
from telemetry.application.tracking import PublicTrack, Tracking, TrackingView, TrackPoint
from telemetry.application.tracking_map import BaseLayer
from telemetry.domain.errors import NoTrackingError
from telemetry.domain.tracking import TrackingLink, TrackingSession

router = APIRouter(prefix="/telemetry", tags=["tracking"])

PUBLIC_HEADERS = {
    "Cache-Control": "no-store",
    "X-Robots-Tag": "noindex, nofollow, noarchive",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
}
"""Headers of every public tracking answer (also the failures)."""


def get_tracking(services: ServicesDep) -> Tracking:
    """The tracking use cases (404 when the mode is not wired)."""
    if services.tracking is None:
        raise NotFound("theft tracking is not available")
    return services.tracking


TrackingDep = Annotated[Tracking, Depends(get_tracking)]


class TrackPointOut(BaseModel):
    """One position of a track."""

    lat: float
    lon: float
    at: datetime

    @classmethod
    def of(cls, point: TrackPoint) -> "TrackPointOut":
        """From a track point."""
        return cls(lat=point.lat, lon=point.lon, at=point.at)


class TrackingSessionOut(BaseModel):
    """A theft tracking session."""

    id: UUID
    mower_id: UUID
    trigger: Literal["mower_lifted", "mower_left_dock_unexpectedly", "geofence_exit", "manual"]
    active: bool
    started_at: datetime
    ends_at: datetime = Field(description="The automatic end (maximum duration).")
    ended_at: datetime | None = None
    end_reason: Literal["false_alarm", "stopped", "max_duration"] | None = None
    interval_seconds: float
    mower_name: str
    mower_model: str | None = None
    mower_serial: str | None = None
    last: TrackPointOut | None = Field(
        default=None, description="Newest position seen by the background tick."
    )
    stale_since: datetime | None = Field(
        default=None, description="Set while the mower has not reported for a while."
    )
    points: int = Field(description="Positions recorded; final once the session ended.")

    @classmethod
    def of(cls, s: TrackingSession) -> "TrackingSessionOut":
        """From a session."""
        last = (
            TrackPointOut(lat=s.last_lat, lon=s.last_lon, at=s.last_fix_at or s.started_at)
            if s.last_lat is not None and s.last_lon is not None
            else None
        )
        return cls(
            id=s.id,
            mower_id=s.mower_id,
            trigger=s.trigger,  # type: ignore[arg-type]
            active=s.active,
            started_at=s.started_at,
            ends_at=s.ends_at,
            ended_at=s.ended_at,
            end_reason=s.end_reason,  # type: ignore[arg-type]
            interval_seconds=s.interval_seconds,
            mower_name=s.mower_name,
            mower_model=s.mower_model,
            mower_serial=s.mower_serial,
            last=last,
            stale_since=s.stale_since,
            points=s.points,
        )


class TrackingLinkOut(BaseModel):
    """A share link (never its token)."""

    id: UUID
    session_id: UUID
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None
    uses: int
    active: bool

    @classmethod
    def of(cls, link: TrackingLink, now: datetime) -> "TrackingLinkOut":
        """From a link."""
        return cls(
            id=link.id,
            session_id=link.session_id,
            created_at=link.created_at,
            expires_at=link.expires_at,
            revoked_at=link.revoked_at,
            last_used_at=link.last_used_at,
            uses=link.uses,
            active=link.usable(now),
        )


class MapLayerOut(BaseModel):
    """A base map layer offered under the track: ``map`` (street map) or ``aerial`` (photo).
    Tiles come from ``/mowers/{id}/tracking/map/{layer}/{z}/{x}/{y}`` (owner) or
    ``POST /public/tracking/tile`` (share link), only around the tracked positions."""

    id: Literal["map", "aerial"]
    attribution: str = Field(description="Credit line to show while the layer is visible.")
    min_zoom: int
    max_zoom: int = Field(description="Highest zoom with real detail; deeper zooms scale it.")
    covers_last: bool = Field(
        default=True,
        description="Whether the source has data at the last known position (false: show "
        "another layer; e.g. an aerial photo of one state only).",
    )
    coverage: AerialBounds | None = Field(
        default=None,
        description="The box the source has data in (null: the whole world). Tiles are served "
        "only where it overlaps the tracked positions plus `margin_m`.",
    )
    margin_m: float = Field(
        default=0,
        description="Metres around the tracked positions where tiles are served; the map keeps "
        "its view inside that box (0: no limit known).",
    )

    @classmethod
    def all(
        cls,
        layers: Sequence[BaseLayer],
        lat: float | None = None,
        lon: float | None = None,
        margin_m: float = 0.0,
    ) -> list["MapLayerOut"]:
        """From the offered layers, judged at the last known position."""
        return [
            cls(
                id=layer.id,
                attribution=layer.attribution,
                min_zoom=layer.min_zoom,
                max_zoom=layer.max_zoom,
                covers_last=layer.covers(lat, lon),
                coverage=(
                    AerialBounds(**dataclasses.asdict(layer.coverage))
                    if layer.coverage is not None
                    else None
                ),
                margin_m=margin_m,
            )
            for layer in layers
        ]


class TrackingStatus(BaseModel):
    """The tracking view of a mower: the session, its track and its links."""

    session: TrackingSessionOut | None = Field(
        description="The active session, else the newest one; null when the mower never had one."
    )
    track: list[TrackPointOut] = Field(default_factory=list)
    truncated: bool = False
    links: list[TrackingLinkOut] = Field(default_factory=list)
    photos: int = 0
    map_layers: list[MapLayerOut] = Field(
        default_factory=list, description="Base map layers under the track; empty without any."
    )
    server_time: datetime

    @classmethod
    def of(
        cls,
        view: TrackingView | None,
        now: datetime,
        layers: Sequence[BaseLayer] = (),
        margin_m: float = 0.0,
    ) -> "TrackingStatus":
        """From a view (None: no session yet)."""
        if view is None:
            return cls(session=None, server_time=now)
        return cls(
            session=TrackingSessionOut.of(view.session),
            track=[TrackPointOut.of(p) for p in view.track],
            truncated=view.truncated,
            links=[TrackingLinkOut.of(link, now) for link in view.links],
            photos=view.photos,
            map_layers=MapLayerOut.all(
                layers, view.session.last_lat, view.session.last_lon, margin_m
            ),
            server_time=now,
        )


class TrackingStopIn(BaseModel):
    """Body of the stop request."""

    reason: Literal["false_alarm", "stopped"] = "stopped"


class TrackingLinkIn(BaseModel):
    """Body of a link request."""

    hours: float | None = Field(
        default=None, gt=0, le=48, description="Lifetime; at most the server's limit (48 h)."
    )
    session_id: UUID | None = None


class TrackingLinkCreated(BaseModel):
    """A new share link. ``token`` is shown only here; the page address is
    ``<app>/track#<token>``."""

    link: TrackingLinkOut
    token: str


class PublicTrackIn(BaseModel):
    """Body of the public view."""

    token: str = Field(min_length=1, max_length=200)


class PublicTrackOut(BaseModel):
    """What a share link shows: nothing else."""

    mower_name: str
    mower_model: str | None = None
    active: bool
    started_at: datetime
    ended_at: datetime | None = None
    last: TrackPointOut | None = None
    stale_since: datetime | None = None
    track: list[TrackPointOut]
    expires_at: datetime
    map_layers: list[MapLayerOut] = Field(default_factory=list)
    server_time: datetime

    @classmethod
    def of(
        cls, p: PublicTrack, layers: Sequence[BaseLayer] = (), margin_m: float = 0.0
    ) -> "PublicTrackOut":
        """From the public view."""
        return cls(
            map_layers=MapLayerOut.all(
                layers, p.last.lat if p.last else None, p.last.lon if p.last else None, margin_m
            ),
            mower_name=p.mower_name,
            mower_model=p.mower_model,
            active=p.active,
            started_at=p.started_at,
            ended_at=p.ended_at,
            last=TrackPointOut.of(p.last) if p.last else None,
            stale_since=p.stale_since,
            track=[TrackPointOut.of(t) for t in p.track],
            expires_at=p.expires_at,
            server_time=p.server_time,
        )


@router.get("/mowers/{mower_id}/tracking", dependencies=[SharedReads])
async def get_tracking_status(
    mower_id: UUID,
    _: ViewMower,
    account_id: CurrentAccountId,
    tracking: TrackingDep,
    session_id: UUID | None = None,
    after: Annotated[
        datetime | None, Query(description="Only positions after this time (polling).")
    ] = None,
) -> TrackingStatus:
    """The active tracking session of the mower, else the newest one, with its track and
    links; ``after`` returns only newer positions."""
    view = await tracking.view(account_id, mower_id, session_id=session_id, after=after)
    return TrackingStatus.of(
        view, tracking_now(tracking), tracking.map_layers, tracking.map_margin_m
    )


def tracking_now(tracking: Tracking) -> datetime:
    """The server time the answers carry (the tracking clock)."""
    return tracking.now()


@router.post("/mowers/{mower_id}/tracking", status_code=201)
async def start_tracking(
    mower_id: UUID,
    principal: ControlMower,
    account_id: CurrentAccountId,
    tracking: TrackingDep,
    response: Response,
) -> TrackingSessionOut:
    """Start tracking by hand (also the safe way to test the whole chain). Answers 200 with the
    running session when there is one."""
    session, created = await tracking.start(
        account_id, mower_id, "manual", user_id=principal.subject
    )
    if not created:
        response.status_code = 200
    return TrackingSessionOut.of(session)


@router.post("/mowers/{mower_id}/tracking/stop")
async def stop_tracking(
    mower_id: UUID,
    _: ControlMower,
    account_id: CurrentAccountId,
    tracking: TrackingDep,
    body: TrackingStopIn,
) -> TrackingSessionOut:
    """End the active session ("Fehlalarm / Ortung beenden")."""
    return TrackingSessionOut.of(await tracking.end(account_id, mower_id, body.reason))


@router.get("/mowers/{mower_id}/tracking/sessions")
async def list_tracking_sessions(
    mower_id: UUID, _: ViewMower, account_id: CurrentAccountId, tracking: TrackingDep
) -> list[TrackingSessionOut]:
    """Past and current sessions of the mower, newest first (at most 20)."""
    return [TrackingSessionOut.of(s) for s in await tracking.sessions(account_id, mower_id)]


@router.delete("/mowers/{mower_id}/tracking/sessions/{session_id}", status_code=204)
async def delete_tracking_session(
    mower_id: UUID,
    session_id: UUID,
    _: ControlMower,
    account_id: CurrentAccountId,
    tracking: TrackingDep,
) -> Response:
    """Delete an ended session with its links and photos."""
    await tracking.delete(account_id, mower_id, session_id)
    return Response(status_code=204)


@router.post("/mowers/{mower_id}/tracking/photo", status_code=201)
async def take_tracking_photo(
    mower_id: UUID,
    _: ControlMower,
    account_id: CurrentAccountId,
    tracking: TrackingDep,
) -> TrackingStatus:
    """Take one more camera photo for the active session (it goes into the report)."""
    view = await tracking.view(account_id, mower_id)
    if view is None or not view.session.active:
        raise NoTrackingError("no active tracking session")
    await tracking.take_photo(account_id, mower_id, view.session.id)
    return TrackingStatus.of(
        await tracking.view(account_id, mower_id),
        tracking_now(tracking),
        tracking.map_layers,
        tracking.map_margin_m,
    )


@router.post("/mowers/{mower_id}/tracking/links", status_code=201)
async def create_tracking_link(
    mower_id: UUID,
    principal: ControlMower,
    account_id: CurrentAccountId,
    tracking: TrackingDep,
    body: TrackingLinkIn,
) -> TrackingLinkCreated:
    """Issue a secret share link (default 48 h). The token is in this answer only."""
    ttl = timedelta(hours=body.hours) if body.hours else None
    link, token = await tracking.create_link(
        account_id, mower_id, session_id=body.session_id, ttl=ttl, user_id=principal.subject
    )
    return TrackingLinkCreated(link=TrackingLinkOut.of(link, tracking_now(tracking)), token=token)


@router.get("/mowers/{mower_id}/tracking/links")
async def list_tracking_links(
    mower_id: UUID,
    _: ViewMower,
    account_id: CurrentAccountId,
    tracking: TrackingDep,
    session_id: UUID | None = None,
) -> list[TrackingLinkOut]:
    """The share links of the session (active ones first by recency; tokens are never shown)."""
    now = tracking_now(tracking)
    return [
        TrackingLinkOut.of(link, now)
        for link in await tracking.links(account_id, mower_id, session_id)
    ]


@router.delete("/mowers/{mower_id}/tracking/links/{link_id}", status_code=204)
async def revoke_tracking_link(
    mower_id: UUID,
    link_id: UUID,
    _: ControlMower,
    account_id: CurrentAccountId,
    tracking: TrackingDep,
) -> Response:
    """Revoke a share link; it stops working at once."""
    del mower_id
    await tracking.revoke_link(account_id, link_id)
    return Response(status_code=204)


@router.get("/mowers/{mower_id}/tracking/report.pdf", response_class=Response)
async def tracking_report(
    mower_id: UUID,
    _: ViewMower,
    account_id: CurrentAccountId,
    tracking: TrackingDep,
    session_id: UUID | None = None,
    lang: Annotated[Literal["de", "en"], Query()] = "en",
) -> Response:
    """The theft report as a PDF: mower, alarm, track (table and drawing), photos, blank owner
    fields."""
    pdf, session = await tracking.report(account_id, mower_id, session_id, lang)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f'attachment; filename="theft-report-{session.started_at:%Y%m%d-%H%M}.pdf"'
            ),
            "Cache-Control": "no-store",
        },
    )


def _client_key(request: Request) -> str:
    """The rate limit key: the client address by the rule every service shares
    (:mod:`mowdeck_service_kit.client_address`, ``MOWDECK_CLIENT_IP_DEPTH``), IPv6 per /64."""
    return limiter_key(client_address(request))


@router.post("/public/tracking/view", tags=["public"])
async def public_tracking_view(
    body: PublicTrackIn, request: Request, response: Response, tracking: TrackingDep
) -> PublicTrackOut:
    """The live page of a share link: the position, the track since the alarm, the times and
    the mower's name and model. No login; 404 for any unusable token, 429 when asked too
    often."""
    response.headers.update(PUBLIC_HEADERS)
    view = await tracking.public_view(body.token, _client_key(request))
    if view is None:
        raise PublicNotFound("not found")
    return PublicTrackOut.of(view, tracking.map_layers, tracking.map_margin_m)


class PublicTileIn(BaseModel):
    """Body of a public base map tile: the share token and the tile (in the body, so neither
    the token nor the place reaches an access log)."""

    token: str = Field(min_length=1, max_length=200)
    layer: Literal["map", "aerial"]
    z: int = Field(ge=0, le=24)
    x: int = Field(ge=0)
    y: int = Field(ge=0)


TILE_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {"content": {"image/jpeg": {}, "image/png": {}}}
}


@router.get(
    "/mowers/{mower_id}/tracking/map/{layer}/{z}/{x}/{y}",
    response_class=Response,
    responses=TILE_RESPONSES,
)
async def tracking_map_tile(
    mower_id: UUID,
    layer: Literal["map", "aerial"],
    z: Annotated[int, Path(ge=0, le=24)],
    x: Annotated[int, Path(ge=0)],
    y: Annotated[int, Path(ge=0)],
    _: ViewMower,
    account_id: CurrentAccountId,
    tracking: TrackingDep,
    session_id: UUID | None = None,
) -> Response:
    """One base map tile (Web Mercator, XYZ) under the track of the active or newest session.
    Tiles away from the tracked positions, outside the zoom range or of a layer that is not
    offered are 404 `tile-outside-map`; new tiles are rate limited per account (429)."""
    tile = await tracking.map_tile(account_id, mower_id, layer, z, x, y, session_id=session_id)
    return Response(
        tile.body,
        media_type=tile.media_type,
        headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"},
    )


@router.post(
    "/public/tracking/tile", tags=["public"], response_class=Response, responses=TILE_RESPONSES
)
async def public_tracking_tile(
    body: PublicTileIn, request: Request, tracking: TrackingDep
) -> Response:
    """One base map tile for the page of a share link, only around the shared track. No
    login; 404 for any unusable token or a tile away from the track, 429 when asked too
    often."""
    tile = await tracking.public_map_tile(
        body.token, _client_key(request), body.layer, body.z, body.x, body.y
    )
    if tile is None:
        raise PublicNotFound("not found")
    return Response(tile.body, media_type=tile.media_type, headers=PUBLIC_HEADERS)


class PublicNotFound(NotFound):
    """An unknown, expired or revoked token (all alike); carries the public headers."""

    def __init__(self, detail: str | None = None) -> None:
        super().__init__(detail, headers=PUBLIC_HEADERS)
