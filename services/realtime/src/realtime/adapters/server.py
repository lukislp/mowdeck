"""The uvicorn server of realtime: WebSockets are closed with 1001 before uvicorn stops.

On shutdown uvicorn closes every open WebSocket itself with 1012 (service restart) before the
app's lifespan shutdown runs. Realtime wants its own close code, so this server first marks the
app as draining (``/readyz`` answers 503) and closes every session with 1001 (going away),
then lets uvicorn continue. The lifespan shutdown hook does the same for sockets that are
still open (tests, other servers).
"""

import socket

import uvicorn
from mowdeck_service_kit import get_logger

_log = get_logger("realtime.server")


class RealtimeServer(uvicorn.Server):
    """A uvicorn server that closes the hub's sessions first."""

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        """Close the sessions with 1001, then shut down like uvicorn."""
        app = self.config.app
        state = getattr(app, "state", None)
        hub = getattr(state, "hub", None)
        if state is not None and hub is not None:
            state.draining = True
            timeout = float(getattr(state, "close_timeout_seconds", 5.0))
            _log.info("closing websockets", count=len(hub.sessions))
            await hub.close_all(timeout=timeout)
        await super().shutdown(sockets)
