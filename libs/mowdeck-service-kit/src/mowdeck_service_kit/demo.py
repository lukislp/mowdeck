"""Public demo mode (ADR 0068): labels every response and refuses the unsafe actions.

A demo installation (``MOWDECK_DEMO_MODE=true``) is a showcase with simulated mowers that anybody
may open without an account. Every service then

* marks every HTTP response with ``X-MowDeck-Demo: 1``, errors included, so clients, proxies and
  people reading logs can tell the demo from a real installation;
* refuses the mutating requests its :class:`DemoPolicy` names with a 403 problem of type
  ``urn:mowdeck:problem:demo-mode`` before any route code runs. Safe methods (GET, HEAD,
  OPTIONS) always pass.

A policy either blocks listed paths (most services: the visitor may try everything but the few
actions that reach third parties or other people) or, with ``allow_only``, blocks every mutation
but the listed ones (Identity: a new endpoint stays closed until someone decides it is safe).

The demo seed job (``infra/compose/docker-compose.demo.yml``) prepares the demo account with
calls that the policy refuses to visitors, for example registering the simulated mowers. It sends
the shared secret ``MOWDECK_DEMO_SEED_TOKEN`` in :data:`DEMO_SEED_HEADER`; such a request passes
the policy. The gateway removes that header from every outside request, and the seed job talks
to the services inside the Compose network only.
"""

import hmac
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from fastapi import Request
from mowdeck_contracts.v1 import PROBLEM_TYPE_PREFIX, ProblemDetails
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mowdeck_service_kit.correlation import get_correlation_id
from mowdeck_service_kit.errors import Forbidden, NotFound, problem_response

DEMO_HEADER = "X-MowDeck-Demo"
"""Response header on every response of a demo installation (value ``1``)."""

DEMO_SEED_HEADER = "X-MowDeck-Demo-Seed"
"""Request header with the seed secret; the gateway strips it from outside requests."""

DEMO_PROBLEM_TYPE = "demo-mode"
"""Problem type (after ``urn:mowdeck:problem:``) of a request the demo refuses."""

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

_ALL_MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass(frozen=True, slots=True)
class DemoRule:
    """Requests ``methods`` on paths matching ``pattern`` (a regular expression over the full
    path, ``/api/v1/...``, matched as a whole) with the reason shown to the visitor."""

    pattern: str
    reason: str
    methods: frozenset[str] = _ALL_MUTATING
    _compiled: re.Pattern[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_compiled", re.compile(self.pattern))

    def matches(self, method: str, path: str) -> bool:
        """Whether this rule covers the request."""
        return method in self.methods and self._compiled.fullmatch(path) is not None


def rule(pattern: str, reason: str, *methods: str) -> DemoRule:
    """A :class:`DemoRule`; without ``methods`` every mutating method."""
    return DemoRule(pattern, reason, frozenset(m.upper() for m in methods) or _ALL_MUTATING)


@dataclass(frozen=True, slots=True)
class DemoPolicy:
    """What one service refuses in demo mode.

    ``blocked``: mutations refused with their rule's reason. ``allow_only``: when not None,
    every other mutation is refused too (with ``default_reason``).
    """

    blocked: tuple[DemoRule, ...] = ()
    allow_only: tuple[DemoRule, ...] | None = None
    default_reason: str = "This action is not available in the demo."

    def refusal(self, method: str, path: str) -> str | None:
        """The reason the demo refuses this request, or None when it may pass."""
        method = method.upper()
        if method in SAFE_METHODS:
            return None
        for item in self.blocked:
            if item.matches(method, path):
                return item.reason
        if self.allow_only is not None and not any(
            item.matches(method, path) for item in self.allow_only
        ):
            return self.default_reason
        return None

    def extended(self, rules: Iterable[DemoRule]) -> "DemoPolicy":
        """This policy with more blocked rules."""
        return DemoPolicy(
            blocked=(*self.blocked, *rules),
            allow_only=self.allow_only,
            default_reason=self.default_reason,
        )


EMPTY_POLICY = DemoPolicy()
"""Only the response header: the service has nothing a visitor could misuse."""


def demo_problem(reason: str, instance: str | None = None) -> ProblemDetails:
    """The 403 problem of a refused demo request."""
    return ProblemDetails(
        type=f"{PROBLEM_TYPE_PREFIX}{DEMO_PROBLEM_TYPE}",
        title="Not available in the demo",
        status=403,
        detail=reason,
        instance=instance,
        correlation_id=get_correlation_id(),
    )


def seed_token_matches(presented: str | None, expected: str | None) -> bool:
    """Constant-time check of the seed secret; never true without a configured secret."""
    if not expected or not presented:
        return False
    return hmac.compare_digest(presented.encode(), expected.encode())


class DemoModeMiddleware:
    """Pure ASGI middleware of a demo installation: :data:`DEMO_HEADER` on every HTTP response
    and the :class:`DemoPolicy` before the routes."""

    def __init__(self, app: ASGIApp, *, policy: DemoPolicy, seed_token: str | None = None) -> None:
        self.app = app
        self.policy = policy
        self.seed_token = seed_token or None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[DEMO_HEADER] = "1"
            await send(message)

        path: str = scope.get("path", "")
        reason = self.policy.refusal(scope.get("method", "GET"), path)
        if reason is not None:
            presented = Headers(scope=scope).get(DEMO_SEED_HEADER)
            if not seed_token_matches(presented, self.seed_token):
                response = problem_response(demo_problem(reason, instance=path))
                await response(scope, receive, send_with_header)
                return
        await self.app(scope, receive, send_with_header)


def demo_enabled(request: Request) -> bool:
    """Whether the app runs in demo mode (``settings.demo_mode``)."""
    settings = getattr(request.app.state, "settings", None)
    return bool(getattr(settings, "demo_mode", False))


def require_demo(request: Request) -> None:
    """FastAPI dependency of demo-only endpoints: 404 outside demo mode."""
    if not demo_enabled(request):
        raise NotFound("there is no demo on this installation")


def require_demo_seed(request: Request) -> None:
    """FastAPI dependency of the seed endpoints: 404 outside demo mode, 403 without the seed
    secret (``MOWDECK_DEMO_SEED_TOKEN``) in :data:`DEMO_SEED_HEADER`."""
    require_demo(request)
    settings = request.app.state.settings
    secret = settings.demo_seed_token.get_secret_value() if settings.demo_seed_token else None
    if not seed_token_matches(request.headers.get(DEMO_SEED_HEADER), secret):
        raise Forbidden("only the demo seed job may call this")
