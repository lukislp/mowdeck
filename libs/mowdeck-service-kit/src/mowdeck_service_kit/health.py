"""Operational endpoints outside ``/api/v1``.

* ``GET /healthz``: liveness, 200 while the process runs.
* ``GET /readyz``: readiness, runs all registered checks; 503 with details on failure and while
  the service is shutting down.
* ``GET /metrics``: Prometheus metrics (when ``metrics_enabled``).
* ``GET /.well-known/mowdeck-module``: the service's :class:`ModuleDescriptor`.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from mowdeck_contracts.v1 import MODULE_DESCRIPTOR_PATH, ModuleDescriptor
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    GCCollector,
    Histogram,
    PlatformCollector,
    ProcessCollector,
    generate_latest,
)
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mowdeck_service_kit.logging import get_logger

_log = get_logger(__name__)

CheckFunction = Callable[[], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class ReadinessCheck:
    """A named readiness check.

    ``check`` fails when it raises or returns ``False``; anything else counts as success. It is
    cancelled after ``timeout_seconds``.
    """

    name: str
    check: CheckFunction
    timeout_seconds: float = 2.0


def as_readiness_check(check: ReadinessCheck | CheckFunction) -> ReadinessCheck:
    """Wrap a bare async callable into a :class:`ReadinessCheck` named after the function."""
    if isinstance(check, ReadinessCheck):
        return check
    return ReadinessCheck(name=getattr(check, "__name__", "check"), check=check)


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    """Result of one readiness check."""

    name: str
    ok: bool
    duration_ms: float
    error: str | None = None


async def run_check(check: ReadinessCheck) -> CheckOutcome:
    """Run one check with its timeout. Error details are logged, only the type is returned."""
    started = time.perf_counter()
    try:
        async with asyncio.timeout(check.timeout_seconds):
            result = await check.check()
    except TimeoutError:
        outcome_error: str | None = "timeout"
        ok = False
    except Exception as exc:
        _log.warning("readiness check failed", check=check.name, error=repr(exc))
        outcome_error = type(exc).__name__
        ok = False
    else:
        ok = result is not False
        outcome_error = None if ok else "check returned false"
    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    return CheckOutcome(name=check.name, ok=ok, duration_ms=duration_ms, error=outcome_error)


def readiness_checks(app: FastAPI) -> list[ReadinessCheck]:
    """The mutable list of readiness checks registered on ``app``."""
    checks: list[ReadinessCheck] = app.state.readiness_checks
    return checks


def add_readiness_check(app: FastAPI, check: ReadinessCheck | CheckFunction) -> None:
    """Register an additional readiness check, e.g. from an ``on_startup`` hook."""
    readiness_checks(app).append(as_readiness_check(check))


def build_health_router(descriptor: ModuleDescriptor, *, metrics: bool) -> APIRouter:
    """Build the router with the operational endpoints."""
    router = APIRouter(tags=["operations"])

    @router.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/readyz", include_in_schema=False)
    async def readyz(request: Request) -> JSONResponse:
        app: FastAPI = request.app
        if getattr(app.state, "draining", False):
            return JSONResponse({"status": "draining", "checks": {}}, status_code=503)
        outcomes = await asyncio.gather(*(run_check(c) for c in readiness_checks(app)))
        status: Literal["ok", "fail"] = "ok" if all(o.ok for o in outcomes) else "fail"
        body = {
            "status": status,
            "checks": {
                o.name: {
                    "status": "ok" if o.ok else "fail",
                    "duration_ms": o.duration_ms,
                    **({"error": o.error} if o.error else {}),
                }
                for o in outcomes
            },
        }
        return JSONResponse(body, status_code=200 if status == "ok" else 503)

    if metrics:

        @router.get("/metrics", include_in_schema=False)
        async def metrics_endpoint(request: Request) -> Response:
            registry: CollectorRegistry = request.app.state.metrics_registry
            return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    @router.get(MODULE_DESCRIPTOR_PATH, response_model=ModuleDescriptor)
    async def module_descriptor() -> ModuleDescriptor:
        return descriptor

    return router


# --- Metrics ----------------------------------------------------------------------------------


def create_metrics_registry() -> CollectorRegistry:
    """A per-app registry with process, platform and GC collectors.

    Services register their own metrics on ``app.state.metrics_registry``.
    """
    registry = CollectorRegistry()
    ProcessCollector(registry=registry)
    PlatformCollector(registry=registry)
    GCCollector(registry=registry)
    return registry


class HttpMetricsMiddleware:
    """Pure ASGI middleware recording request counts and latencies per route template.

    The route label is the full path template (``/api/v1/mowers/{mower_id}``), never the
    concrete path, so label cardinality stays bounded. ``route_templates`` maps ``id(route)``
    to its full template; it is filled by :func:`~mowdeck_service_kit.app.create_app`
    because included routers keep router-relative templates.
    """

    def __init__(
        self,
        app: ASGIApp,
        registry: CollectorRegistry,
        skip_paths: Iterable[str] = (),
        route_templates: Mapping[int, str] | None = None,
    ) -> None:
        self.app = app
        self.skip_paths = frozenset(skip_paths)
        self.route_templates = route_templates if route_templates is not None else {}
        self.requests = Counter(
            "http_requests_total",
            "HTTP requests by method, route template and status code.",
            ["method", "route", "status"],
            registry=registry,
        )
        self.latency = Histogram(
            "http_request_duration_seconds",
            "HTTP request latency by method and route template.",
            ["method", "route"],
            registry=registry,
        )

    def _template(self, scope: Scope) -> str:
        route = scope.get("route")
        if route is None:
            return "<unmatched>"
        known = self.route_templates.get(id(route))
        return known or str(getattr(route, "path_format", None) or "<unmatched>")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in self.skip_paths:
            await self.app(scope, receive, send)
            return
        status_code = 500
        started = time.perf_counter()

        async def recording_send(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, recording_send)
        finally:
            template = self._template(scope)
            method = str(scope.get("method", "GET"))
            self.requests.labels(method, template, str(status_code)).inc()
            self.latency.labels(method, template).observe(time.perf_counter() - started)
