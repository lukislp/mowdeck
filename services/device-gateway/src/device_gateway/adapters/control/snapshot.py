"""``CameraSnapshotter`` over HTTP: the Vision image's capture worker (ADR 0035).

``POST <MOWDECK_CAPTURE_URL>/api/v1/capture`` with a
:class:`~mowdeck_contracts.v1.CameraSnapshotRequest` (the camera session including its secret
token, the camera's Agora uid, the frame timeout) and ``Authorization: Bearer
<MOWDECK_CAPTURE_TOKEN>`` when a token is configured; the answer is the JPEG. The token of the
session and the image are never logged; problems become
:class:`~device_gateway.application.ports.SnapshotFailed` with the worker's status only.
"""

import httpx
from mowdeck_contracts.v1 import CameraSession, CameraSnapshotRequest
from mowdeck_control import Photo
from mowdeck_service_kit import get_logger

from device_gateway.application.ports import SnapshotFailed

_log = get_logger(__name__)


class HttpCameraSnapshotter:
    """Asks the capture worker for one frame."""

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

    async def snapshot(
        self, session: CameraSession, camera_uid: int | None, *, timeout_seconds: float
    ) -> Photo:
        """One JPEG; the HTTP timeout leaves the worker a few seconds beyond its own."""
        body = CameraSnapshotRequest(
            session=session,
            camera_uid=camera_uid,
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
        media_type = response.headers.get("content-type", "").split(";")[0].strip()
        if response.status_code != httpx.codes.OK or media_type != "image/jpeg":
            _log.info("capture worker refused", status=response.status_code)
            raise SnapshotFailed(f"the capture worker answered {response.status_code}")
        if not response.content:
            raise SnapshotFailed("the capture worker sent an empty image")
        return Photo(response.content, "image/jpeg")
