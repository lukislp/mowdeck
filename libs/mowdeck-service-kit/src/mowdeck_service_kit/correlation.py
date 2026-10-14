"""Correlation ids: one id per request or message, carried through all services.

The id is read from ``X-Correlation-ID`` (or ``X-Request-ID``), generated when missing or
malformed, stored in a context variable, added to every log line and echoed in the response.
Bus helpers put it on outgoing messages and restore it when handling incoming ones.
"""

import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4

import structlog
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CORRELATION_HEADER = "X-Correlation-ID"
REQUEST_ID_HEADER = "X-Request-ID"

_VALID = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")

correlation_id_var: ContextVar[str | None] = ContextVar("mowdeck_correlation_id", default=None)


def new_correlation_id() -> str:
    """Return a fresh correlation id (a random UUID)."""
    return str(uuid4())


def get_correlation_id() -> str | None:
    """Return the correlation id of the current request or message, if any."""
    return correlation_id_var.get()


def is_valid_correlation_id(value: str | None) -> bool:
    """Return True if ``value`` is safe to accept from a client (charset, max 128 chars)."""
    return value is not None and _VALID.match(value) is not None


@contextmanager
def correlation_scope(correlation_id: str | None = None) -> Iterator[str]:
    """Set the correlation id for the enclosed block (a new one if missing or invalid)."""
    valid = correlation_id is not None and _VALID.match(correlation_id) is not None
    value = correlation_id if valid and correlation_id is not None else new_correlation_id()
    token = correlation_id_var.set(value)
    try:
        yield value
    finally:
        correlation_id_var.reset(token)


class CorrelationIdMiddleware:
    """Pure ASGI middleware that manages the correlation id of HTTP requests.

    Structlog context variables bound while handling the request (for example the account id
    bound by the tenancy dependencies) are restored afterwards, so they never leak into the
    next request handled in the same context.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = _header(scope, CORRELATION_HEADER) or _header(scope, REQUEST_ID_HEADER)
        saved_log_context = structlog.contextvars.get_contextvars()
        with correlation_scope(incoming) as correlation_id:

            async def send_with_header(message: Message) -> None:
                if message["type"] == "http.response.start":
                    headers = MutableHeaders(scope=message)
                    headers[CORRELATION_HEADER] = correlation_id
                await send(message)

            try:
                await self.app(scope, receive, send_with_header)
            finally:
                structlog.contextvars.clear_contextvars()
                structlog.contextvars.bind_contextvars(**saved_log_context)


def _header(scope: Scope, name: str) -> str | None:
    wanted = name.lower().encode("latin-1")
    for key, value in scope.get("headers", []):
        if key.lower() == wanted:
            decoded: str = value.decode("latin-1")
            return decoded
    return None
