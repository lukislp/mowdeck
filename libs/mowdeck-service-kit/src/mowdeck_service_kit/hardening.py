"""Hardening headers on every service response.

* ``X-Content-Type-Options: nosniff`` on every HTTP response: a browser never guesses another
  type than the one the service declares (JSON, an image, a PDF).
* ``Cache-Control: no-store`` on responses to a request with an ``Authorization`` header,
  unless the route set its own ``Cache-Control``: what a caller may see is never kept by a
  browser or a cache on the way. Routes that allow reuse say so themselves (aerial tiles,
  error lexicons, the widget summary, ETag'd maps) and keep their header.

WebSocket connections and lifespan events pass untouched.
"""

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

NO_STORE = "no-store"
NOSNIFF = "nosniff"


class HardeningHeadersMiddleware:
    """Pure ASGI middleware adding the hardening headers (see the module)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        authenticated = "authorization" in Headers(scope=scope)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if "x-content-type-options" not in headers:
                    headers["X-Content-Type-Options"] = NOSNIFF
                if authenticated and "cache-control" not in headers:
                    headers["Cache-Control"] = NO_STORE
            await send(message)

        await self.app(scope, receive, send_with_headers)
