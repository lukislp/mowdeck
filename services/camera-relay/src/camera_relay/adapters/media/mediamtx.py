"""MediaMTX's control API (``/v3``), reachable by the relay only, and the ``MediaPlane`` port.

The relay authenticates with the internal publisher credential (MediaMTX asks the relay's own
auth callback, which allows ``api`` only with it). Used endpoints:

* ``GET /v3/paths/list``: which relay paths have a ready stream (source readiness);
* ``GET /v3/webrtcsessions/list``: the WHEP readers with their ``bytesSent`` (media
  confirmation);
* ``POST /v3/webrtcsessions/kick/{id}``: end a reader.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
from mowdeck_service_kit import get_logger

from camera_relay.application.media_auth import WhepBindings

_log = get_logger("camera_relay.media.mediamtx")

PAGE_SIZE = 500


class MediaServerError(Exception):
    """The control API failed or answered unexpectedly."""


@dataclass(frozen=True, slots=True)
class WebRtcSession:
    """One WHEP reader as MediaMTX reports it."""

    id: str
    path: str
    state: str
    bytes_sent: int
    established: bool


@dataclass(frozen=True, slots=True)
class MediaPath:
    """One path with its readiness."""

    name: str
    ready: bool


class MediaMtxApi:
    """A small client of the control API."""

    def __init__(
        self,
        base_url: str,
        *,
        user: str,
        secret: Callable[[], str],
        timeout: float = 3.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout, transport=transport
        )
        self._user = user
        self._secret = secret

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()

    async def _request(self, method: str, url: str) -> httpx.Response:
        try:
            response = await self._client.request(
                method, url, auth=httpx.BasicAuth(self._user, self._secret())
            )
        except httpx.HTTPError as exc:
            msg = f"media server API {method} {url} failed: {type(exc).__name__}"
            raise MediaServerError(msg) from exc
        return response

    async def _items(self, url: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 0
        while True:
            response = await self._request("GET", f"{url}?itemsPerPage={PAGE_SIZE}&page={page}")
            if response.status_code != 200:
                msg = f"media server API GET {url}: HTTP {response.status_code}"
                raise MediaServerError(msg)
            body = response.json()
            items.extend(body.get("items") or [])
            page += 1
            if page >= int(body.get("pageCount") or 0):
                return items

    async def webrtc_sessions(self) -> list[WebRtcSession]:
        """Every WHEP/WHIP session."""
        return [
            WebRtcSession(
                id=str(item["id"]),
                path=str(item.get("path", "")),
                state=str(item.get("state", "")),
                bytes_sent=int(item.get("bytesSent") or item.get("outboundBytes") or 0),
                established=bool(item.get("peerConnectionEstablished")),
            )
            for item in await self._items("/v3/webrtcsessions/list")
        ]

    async def paths(self) -> list[MediaPath]:
        """Every path with a publisher or a reader."""
        return [
            MediaPath(name=str(item["name"]), ready=bool(item.get("ready")))
            for item in await self._items("/v3/paths/list")
        ]

    async def kick_webrtc_session(self, session_id: str) -> bool:
        """End a WHEP session; False when it no longer exists."""
        response = await self._request("POST", f"/v3/webrtcsessions/kick/{session_id}")
        if response.status_code == 404:
            return False
        if response.status_code != 200:
            msg = f"media server API kick: HTTP {response.status_code}"
            raise MediaServerError(msg)
        return True


class MediaMtxPlane:
    """The ``MediaPlane`` port: ends a viewer's WHEP session and forgets its binding."""

    def __init__(self, api: MediaMtxApi, bindings: WhepBindings) -> None:
        self.api = api
        self.bindings = bindings

    async def kick(self, whep_session: str) -> None:
        """End one reader session."""
        self.bindings.unbind(whep_session)
        kicked = await self.api.kick_webrtc_session(whep_session)
        _log.info("viewer kicked", whep_session=whep_session, existed=kicked)
