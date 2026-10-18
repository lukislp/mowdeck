"""The ``CameraSnapshotter`` port over HTTP: the camera relay's frame tap (ADR 0045) and the
deprecated capture worker (ADR 0035).

``RelaySnapshotter``: ``POST <MOWDECK_RELAY_URL>/internal/v1/frames/snapshot`` with the mower's
account and id (from the gateway's own connection), ``max_width``, ``max_bytes`` and
``timeout_s``, the gateway's service principal as ``requested_by`` and ``Authorization: Bearer
<MOWDECK_RELAY_FRAMES_TOKEN>``; the answer is the JPEG. The relay holds a ``vision`` lease for
exactly the request: it reuses the session a viewer holds (no second vendor session) or starts
one within the Vision limits. A relay that is not reachable, has the frame tap switched off
(``frames-disabled``) or no source driver for the camera (``camera-relay-unsupported``) is
:class:`~device_gateway.application.ports.SnapshotUnavailable`, so the gateway may try the
capture worker; every other answer (a limit, an offline mower, no frame in time) is final.

``HttpCameraSnapshotter`` (deprecated with ADR 0045, kept for one release):
``POST <MOWDECK_CAPTURE_URL>/api/v1/capture`` with a
:class:`~mowdeck_contracts.v1.CameraSnapshotRequest` (the camera session including its secret
token, the camera's Agora uid, the frame timeout) and ``Authorization: Bearer
<MOWDECK_CAPTURE_TOKEN>`` when a token is configured; the answer is the JPEG.

Tokens and images are never logged; problems carry the HTTP status only.
"""

from typing import Final
from uuid import UUID

import httpx
from mowdeck_contracts.v1 import CameraSnapshotRequest, PrincipalKind, PrincipalRef
from mowdeck_contracts.v1.vision import PHOTO_MAX_BYTES
from mowdeck_control import Photo
from mowdeck_service_kit import get_logger

from device_gateway.application.ports import SnapshotFailed, SnapshotTarget, SnapshotUnavailable

_log = get_logger(__name__)

GATEWAY_SERVICE_SUBJECT: Final = UUID("2b7e51c4-6a9d-4f38-8e0c-41d5b3a7c962")
"""Subject of the gateway's service principal towards the relay (owns the Vision leases)."""
HANDOVER_PROBLEMS: Final = frozenset({"frames-disabled", "camera-relay-unsupported"})
"""Problem types of the relay after which another frame source may be tried."""
RELAY_MAX_TIMEOUT_SECONDS: Final = 30.0
"""The relay holds a snapshot lease at most this long."""


def _jpeg(response: httpx.Response, source: str) -> Photo:
    media_type = response.headers.get("content-type", "").split(";")[0].strip()
    if response.status_code != httpx.codes.OK or media_type != "image/jpeg":
        raise SnapshotFailed(f"the {source} answered {response.status_code}")
    if not response.content:
        raise SnapshotFailed(f"the {source} sent an empty image")
    return Photo(response.content, "image/jpeg")


def _problem(response: httpx.Response) -> tuple[str, str]:
    """The problem type's last part and the detail of a problem document (or empty)."""
    try:
        body = response.json()
    except ValueError:
        return "", ""
    if not isinstance(body, dict):
        return "", ""
    kind = str(body.get("type", "")).rsplit(":", 1)[-1]
    detail = str(body.get("detail") or "")[:300]
    return kind, detail


class RelaySnapshotter:
    """Asks the camera relay's frame tap for one frame (ADR 0045)."""

    needs_camera_session = False

    def __init__(
        self,
        base_url: str,
        *,
        token: str,
        max_width: int = 1280,
        max_bytes: int = PHOTO_MAX_BYTES,
        principal: PrincipalRef | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.url = base_url.rstrip("/") + "/internal/v1/frames/snapshot"
        self.token = token
        self.max_width = max_width
        self.max_bytes = max_bytes
        self.principal = principal or PrincipalRef(
            subject=GATEWAY_SERVICE_SUBJECT,
            kind=PrincipalKind.SERVICE,
            display_name="device-gateway",
        )
        self._transport = transport

    async def snapshot(self, target: SnapshotTarget, *, timeout_seconds: float) -> Photo:
        """One JPEG of the mower's camera through the relay."""
        wait = min(timeout_seconds, RELAY_MAX_TIMEOUT_SECONDS)
        body = {
            "account_id": str(target.account_id),
            "mower_id": str(target.mower_id),
            "max_width": self.max_width,
            "max_bytes": self.max_bytes,
            "timeout_s": wait,
            "requested_by": self.principal.model_dump(mode="json", exclude_none=True),
        }
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=wait + 5) as client:
                response = await client.post(self.url, json=body, headers=headers)
        except httpx.TimeoutException as exc:
            raise SnapshotFailed("the camera relay did not answer in time") from exc
        except httpx.HTTPError as exc:
            raise SnapshotUnavailable(
                f"the camera relay is unreachable: {type(exc).__name__}"
            ) from exc
        if response.status_code == httpx.codes.OK:
            return _jpeg(response, "camera relay")
        problem, detail = _problem(response)
        _log.info("camera relay refused the frame", status=response.status_code, problem=problem)
        text = f"the camera relay answered {response.status_code} {problem}".strip()
        if detail:
            text = f"{text}: {detail}"
        if problem in HANDOVER_PROBLEMS:
            raise SnapshotUnavailable(text)
        if response.status_code in (401, 405) or (response.status_code == 404 and not problem):
            # The tokens do not match, or a relay without the frame tap: a configuration the
            # next source may cover. A refused mower (a "not-found" problem) stays final.
            raise SnapshotUnavailable(text)
        raise SnapshotFailed(text)


class HttpCameraSnapshotter:
    """Asks the capture worker for one frame (deprecated, ADR 0035)."""

    needs_camera_session = True

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        max_width: int = 1280,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.url = base_url.rstrip("/") + "/api/v1/capture"
        self.token = token or None
        self.max_width = max_width
        self._transport = transport

    async def snapshot(self, target: SnapshotTarget, *, timeout_seconds: float) -> Photo:
        """One JPEG; the HTTP timeout leaves the worker a few seconds beyond its own."""
        if target.session is None:
            raise SnapshotUnavailable("the capture worker needs a camera session")
        body = CameraSnapshotRequest(
            session=target.session,
            camera_uid=target.camera_uid,
            timeout_seconds=timeout_seconds,
            max_width=self.max_width,
        )
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=timeout_seconds + 5
            ) as client:
                response = await client.post(
                    self.url, content=body.model_dump_json(), headers=headers
                )
        except httpx.TimeoutException as exc:
            raise SnapshotFailed("the capture worker did not answer in time") from exc
        except httpx.HTTPError as exc:
            raise SnapshotFailed(
                f"the capture worker is unreachable: {type(exc).__name__}"
            ) from exc
        if response.status_code != httpx.codes.OK:
            _log.info("capture worker refused", status=response.status_code)
        return _jpeg(response, "capture worker")
