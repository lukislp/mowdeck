"""The WebSocket endpoint ``GET /api/v1/realtime/ws``.

The route only adapts Starlette's WebSocket to the :class:`~realtime.application.ports.Socket`
port and hands the connection to the hub; the protocol lives in
:mod:`realtime.application.session`. No token travels in the URL or in headers: the client
sends it in the first message.
"""

from typing import cast

from fastapi import APIRouter, WebSocket
from mowdeck_contracts.v1.realtime import RealtimeCloseCode
from mowdeck_service_kit import get_logger
from mowdeck_service_kit.client_address import client_address
from starlette.requests import Request
from starlette.websockets import WebSocketState

from realtime.application.hub import Hub
from realtime.application.session import Receive
from realtime.domain.errors import FrameRejectedError

router = APIRouter(prefix="/realtime", tags=["realtime"])
_log = get_logger("realtime.api")

POLICY_VIOLATION = 1008
"""Close code of a refused origin (RFC 6455)."""


class StarletteSocket:
    """The ``Socket`` port over a Starlette WebSocket."""

    def __init__(self, websocket: WebSocket) -> None:
        self.websocket = websocket

    async def send_text(self, text: str) -> None:
        """Send one text frame."""
        await self.websocket.send_text(text)

    async def close(self, code: int, reason: str) -> None:
        """Send the close frame unless the socket is closed already."""
        if self.websocket.application_state != WebSocketState.DISCONNECTED:
            await self.websocket.close(code=code, reason=reason)


def handshake_address(websocket: WebSocket) -> str | None:
    """The client address of the handshake, by the one rule every service uses
    (:mod:`mowdeck_service_kit.client_address`, ``MOWDECK_CLIENT_IP_DEPTH``)."""
    # client_address reads only what every HTTP connection has (client, headers, app).
    return client_address(cast("Request", websocket))


def receiver(websocket: WebSocket, max_bytes: int) -> Receive:
    """Text frames of the socket; None when the client left."""

    async def receive() -> str | None:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return None
        text = message.get("text")
        if text is None:
            if len(message.get("bytes") or b"") > max_bytes:
                raise FrameRejectedError(RealtimeCloseCode.MESSAGE_TOO_BIG, "message too big")
            raise FrameRejectedError(RealtimeCloseCode.PROTOCOL_ERROR, "text frames only")
        return str(text)

    return receive


@router.websocket("/ws")
async def realtime_socket(websocket: WebSocket) -> None:
    """Live states and command results of the mowers the principal may view."""
    hub: Hub = websocket.app.state.hub
    allowed: frozenset[str] = websocket.app.state.allowed_origins
    origin = websocket.headers.get("origin")
    if allowed and origin is not None and origin not in allowed:
        # Say which origin was refused: a phone opening the app on a LAN address is refused
        # silently otherwise (add that address to REALTIME_ALLOWED_ORIGINS).
        _log.info(
            "websocket refused: origin not allowed",
            origin=origin,
            allowed=sorted(allowed),
            hint="add it to REALTIME_ALLOWED_ORIGINS",
        )
        await websocket.close(code=POLICY_VIOLATION)
        return
    await websocket.accept()
    session = hub.open(StarletteSocket(websocket), address=handshake_address(websocket))
    await session.serve(receiver(websocket, hub.config.max_message_bytes))
