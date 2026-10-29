"""Authentication and request pacing for the Automower Connect API.

:class:`TokenSource` exchanges the user's application key and secret for a bearer token with the
OAuth2 client-credentials grant (``POST https://api.authentication.husqvarnagroup.dev/v1/oauth2/
token``) and caches it until :data:`TOKEN_MARGIN_S` before it expires, so one application key
asks for a token about once a day (Husqvarna's tokens live 24 hours). A refused request raises
:class:`~mowdeck_control.CloudLoginRefused`, a network failure, a rate limit or a server error
:class:`~mowdeck_control.CloudUnavailable`; neither message contains the key, the secret or the
vendor's answer.

:class:`ClientCredentialsAuth` is aioautomower's ``AbstractAuth`` on top of it: aioautomower's
session and commands call ``get_json``, ``post_json`` and ``patch_json``, which go through the
:class:`~mowdeck_control_husqvarna.transport.HttpTransport` (never through aiohttp directly) and
through a :class:`RequestThrottle` that keeps one application key at no more than one request
per second (Husqvarna's limit). An emergency stop skips the wait (:func:`urgent`).
"""

import asyncio
import contextlib
import contextvars
import logging
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping
from typing import Any, Final, cast

from aioautomower.auth import AbstractAuth
from aioautomower.const import API_BASE_URL, AUTH_API_TOKEN_URL, WS_URL
from aioautomower.exceptions import (
    ApiBadRequestError,
    ApiError,
    ApiForbiddenError,
    ApiUnauthorizedError,
    HusqvarnaWSClientError,
)
from aiohttp import ClientResponse, ClientSession, ClientWebSocketResponse
from mowdeck_control import CloudLoginRefused, CloudUnavailable

from mowdeck_control_husqvarna.transport import HttpTransport, TransportError

_log = logging.getLogger("mowdeck_control_husqvarna.auth")

TOKEN_URL: Final = AUTH_API_TOKEN_URL
"""``https://api.authentication.husqvarnagroup.dev/v1/oauth2/token``."""
TOKEN_MARGIN_S: Final = 300.0
"""A cached token is renewed this long before it expires."""
DEFAULT_TOKEN_LIFETIME_S: Final = 3600.0
"""Lifetime assumed when the token answer names none."""
MIN_REQUEST_INTERVAL_S: Final = 1.0
"""Husqvarna allows one request per second per application key."""

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]

_URGENT: contextvars.ContextVar[bool] = contextvars.ContextVar("husqvarna_urgent", default=False)


@contextlib.contextmanager
def urgent() -> Iterator[None]:
    """Requests made inside skip the throttle's wait (the emergency stop)."""
    reset = _URGENT.set(True)
    try:
        yield
    finally:
        _URGENT.reset(reset)


class RateLimitedError(ApiError):
    """Husqvarna answered 429: more than one request per second or the monthly quota."""


class TokenSource:
    """The bearer token of one application, cached until shortly before it expires."""

    def __init__(
        self,
        transport: HttpTransport,
        app_key: str,
        app_secret: str,
        *,
        clock: Clock = time.monotonic,
        margin_s: float = TOKEN_MARGIN_S,
    ) -> None:
        self._transport = transport
        self.app_key = app_key
        self._secret = app_secret
        self._clock = clock
        self._margin_s = margin_s
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()
        self.requests = 0
        """Token requests sent so far (tests and metrics)."""

    def __repr__(self) -> str:
        return "TokenSource(<application key and secret hidden>)"

    def invalidate(self) -> None:
        """Forget the cached token (the API answered 401 with it)."""
        self._token = None
        self._expires_at = 0.0

    async def token(self) -> str:
        """A valid bearer token, from the cache when it is not about to expire."""
        async with self._lock:
            if self._token is not None and self._clock() < self._expires_at - self._margin_s:
                return self._token
            self._token, lifetime = await self._fetch()
            self._expires_at = self._clock() + lifetime
            return self._token

    async def _fetch(self) -> tuple[str, float]:
        self.requests += 1
        form = {
            "grant_type": "client_credentials",
            "client_id": self.app_key,
            "client_secret": self._secret,
        }
        try:
            reply = await self._transport.post_form(TOKEN_URL, form)
        except TransportError as exc:
            msg = "the Husqvarna authentication service did not answer"
            raise CloudUnavailable(msg) from exc
        if reply.status in (400, 401, 403):
            msg = "Husqvarna refused the application key or secret"
            raise CloudLoginRefused(msg)
        if reply.status == 429:
            msg = "Husqvarna's rate limit for this application key is reached; try again later"
            raise CloudUnavailable(msg)
        body = reply.body if isinstance(reply.body, dict) else {}
        token = body.get("access_token")
        if reply.status >= 300 or not isinstance(token, str) or not token:
            msg = f"the Husqvarna authentication service failed (HTTP {reply.status})"
            raise CloudUnavailable(msg)
        lifetime = body.get("expires_in")
        if isinstance(lifetime, bool) or not isinstance(lifetime, int | float) or lifetime <= 0:
            lifetime = DEFAULT_TOKEN_LIFETIME_S
        return token, float(lifetime)


class RequestThrottle:
    """Spaces the requests of one application key at least ``interval_s`` apart."""

    def __init__(
        self,
        interval_s: float = MIN_REQUEST_INTERVAL_S,
        *,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._interval_s = interval_s
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._last: float | None = None
        self.requests = 0
        """Requests paced so far (tests and metrics)."""

    async def wait(self) -> None:
        """Return when the next request may go out (at once inside :func:`urgent`)."""
        if _URGENT.get():
            self._mark()
            return
        async with self._lock:
            if self._last is not None:
                delay = self._last + self._interval_s - self._clock()
                if delay > 0:
                    await self._sleep(delay)
            self._mark()

    def _mark(self) -> None:
        self.requests += 1
        self._last = self._clock()


_STATUS_ERRORS: Final[Mapping[int, type[Exception]]] = {
    400: ApiBadRequestError,
    401: ApiUnauthorizedError,
    403: ApiForbiddenError,
    429: RateLimitedError,
}


class ClientCredentialsAuth(AbstractAuth):
    """aioautomower's ``AbstractAuth`` on a :class:`TokenSource` and an
    :class:`~mowdeck_control_husqvarna.transport.HttpTransport`."""

    def __init__(
        self,
        transport: HttpTransport,
        tokens: TokenSource,
        *,
        throttle: RequestThrottle | None = None,
    ) -> None:
        # The aiohttp session of AbstractAuth is never used: every request goes through the
        # transport (see request()).
        super().__init__(cast(ClientSession, None), API_BASE_URL)
        self._transport = transport
        self._tokens = tokens
        self.throttle = throttle or RequestThrottle()
        # The API key header is the application key; no need to decode it from the token.
        self._client_id = tokens.app_key

    def __repr__(self) -> str:
        return "ClientCredentialsAuth(<credentials hidden>)"

    async def async_get_access_token(self) -> str:
        return await self._tokens.token()

    async def request(self, method: str, url: str, **kwargs: Any) -> ClientResponse:
        msg = "requests go through the MowDeck transport (get_json, post_json, patch_json)"
        raise NotImplementedError(msg)

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        return await self._json("get", url, kwargs.get("json"))

    async def post_json(self, url: str, **kwargs: Any) -> dict[str, Any]:
        return await self._json("post", url, kwargs.get("json"))

    async def patch_json(self, url: str, **kwargs: Any) -> dict[str, Any]:
        return await self._json("patch", url, kwargs.get("json"))

    async def _json(self, method: str, url: str, body: Any) -> dict[str, Any]:
        if not url.startswith(("http://", "https://")):
            url = f"{API_BASE_URL}/{url}"
        for attempt in (1, 2):
            await self.throttle.wait()
            headers = await self.headers()
            try:
                reply = await self._transport.request_json(
                    method, url, headers=headers, json_body=body
                )
            except TransportError as exc:
                raise ApiError(str(exc)) from exc
            if reply.status == 401 and attempt == 1:
                # The token may have been revoked before its time: one retry with a new one.
                _log.info("Husqvarna answered 401; renewing the token once")
                self._tokens.invalidate()
                continue
            error = _STATUS_ERRORS.get(reply.status)
            if error is not None:
                raise error(f"HTTP {reply.status}")
            if reply.status >= 300:
                msg = f"HTTP {reply.status}"
                raise ApiError(msg)
            return reply.body if isinstance(reply.body, dict) else {}
        raise ApiUnauthorizedError("HTTP 401")  # pragma: no cover - the loop returns or raises

    async def websocket_connect(self) -> None:
        token = await self._tokens.token()
        try:
            ws = await self._transport.ws_connect(
                WS_URL, headers={"Authorization": f"Bearer {token}"}
            )
        except TransportError as exc:
            raise HusqvarnaWSClientError(str(exc)) from exc
        # The transport's socket has the subset of ClientWebSocketResponse aioautomower uses.
        self.ws = cast(ClientWebSocketResponse, ws)
