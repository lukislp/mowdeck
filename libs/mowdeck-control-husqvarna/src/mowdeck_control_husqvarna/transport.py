"""The HTTP and websocket port of the adapter.

Every byte the adapter exchanges with Husqvarna goes through an :class:`HttpTransport`: the token
request (:mod:`~mowdeck_control_husqvarna.auth`), the REST calls aioautomower makes and the
websocket it listens on. :class:`AiohttpTransport` is the real one; the tests and the fake cloud
(:mod:`~mowdeck_control_husqvarna.fake_cloud`) answer from memory, so nothing in the tests
touches the network.

A transport never logs request bodies or headers (they hold the secret or the bearer token) and
raises :class:`TransportError` for network failures, with a message that names only the kind of
failure.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import aiohttp

REQUEST_TIMEOUT_S = 30.0
"""Upper bound of one HTTP request."""
WS_HEARTBEAT_S = 60.0
"""aiohttp's websocket ping interval (aioautomower uses the same)."""


class TransportError(Exception):
    """The request did not reach Husqvarna or no answer came back (network, timeout)."""


@dataclass(frozen=True, slots=True)
class HttpReply:
    """Status and parsed JSON body (None when the body was empty or not JSON)."""

    status: int
    body: Any = None


class WebSocketLike(Protocol):
    """What aioautomower's session uses of an aiohttp ``ClientWebSocketResponse``."""

    @property
    def closed(self) -> bool:
        """True once the socket closed."""
        ...

    async def receive(self, timeout: float | None = None) -> aiohttp.WSMessage:  # noqa: ASYNC109 - aiohttp's signature
        """The next message; raises ``TimeoutError`` after ``timeout`` seconds."""
        ...

    async def send_str(self, data: str, compress: int | None = None) -> None:
        """Send a text frame (aioautomower's keepalive sends an empty one)."""
        ...

    async def close(self, *, code: int = 1000, message: bytes = b"") -> bool:
        """Close the socket."""
        ...


class HttpTransport(Protocol):
    """HTTP and websocket access to Husqvarna."""

    async def post_form(self, url: str, form: Mapping[str, str]) -> HttpReply:
        """POST ``application/x-www-form-urlencoded`` (the token request)."""
        ...

    async def request_json(
        self, method: str, url: str, *, headers: Mapping[str, str], json_body: Any = None
    ) -> HttpReply:
        """A JSON:API request of the Automower Connect API."""
        ...

    async def ws_connect(self, url: str, *, headers: Mapping[str, str]) -> WebSocketLike:
        """Open the event websocket."""
        ...

    async def close(self) -> None:
        """Release the connections."""
        ...


def _parse(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


class AiohttpTransport:
    """:class:`HttpTransport` on one ``aiohttp.ClientSession`` (created on first use)."""

    def __init__(self, *, timeout_s: float = REQUEST_TIMEOUT_S) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._session: aiohttp.ClientSession | None = None

    def _client(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def post_form(self, url: str, form: Mapping[str, str]) -> HttpReply:
        try:
            async with self._client().post(
                url, data=dict(form), headers={"Accept": "application/json"}
            ) as resp:
                return HttpReply(resp.status, _parse(await resp.read()))
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise TransportError(f"token request failed: {type(exc).__name__}") from None

    async def request_json(
        self, method: str, url: str, *, headers: Mapping[str, str], json_body: Any = None
    ) -> HttpReply:
        try:
            async with self._client().request(
                method.upper(), url, headers=dict(headers), json=json_body
            ) as resp:
                return HttpReply(resp.status, _parse(await resp.read()))
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise TransportError(f"{method.upper()} failed: {type(exc).__name__}") from None

    async def ws_connect(self, url: str, *, headers: Mapping[str, str]) -> WebSocketLike:
        try:
            return await self._client().ws_connect(
                url, headers=dict(headers), heartbeat=WS_HEARTBEAT_S
            )
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise TransportError(f"websocket failed: {type(exc).__name__}") from None

    async def close(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            await session.close()
