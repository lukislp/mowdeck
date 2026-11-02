"""Application factory: one call builds a fully equipped MowDeck service."""

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Request
from mowdeck_contracts.v1 import ModuleDescriptor

from mowdeck_service_kit.bus import EventBus, JetStreamBus
from mowdeck_service_kit.correlation import CorrelationIdMiddleware
from mowdeck_service_kit.demo import EMPTY_POLICY, DemoModeMiddleware, DemoPolicy
from mowdeck_service_kit.errors import (
    ServiceUnavailable,
    UnhandledErrorMiddleware,
    install_error_handlers,
)
from mowdeck_service_kit.hardening import HardeningHeadersMiddleware
from mowdeck_service_kit.health import (
    CheckFunction,
    HttpMetricsMiddleware,
    ReadinessCheck,
    ReadinessMetrics,
    add_readiness_check,
    as_readiness_check,
    build_health_router,
    create_metrics_registry,
    watch_readiness,
)
from mowdeck_service_kit.logging import configure_logging, get_logger
from mowdeck_service_kit.otel import instrument_app
from mowdeck_service_kit.settings import ServiceSettings
from mowdeck_service_kit.source import SourceCodeMiddleware
from mowdeck_service_kit.tenancy import PrincipalResolver

API_PREFIX = "/api/v1"
"""Prefix under which all service routers are mounted."""

LifecycleHook = Callable[[FastAPI], Awaitable[None]]
"""An async startup or shutdown hook receiving the app (state lives on ``app.state``)."""

if TYPE_CHECKING:
    import uvicorn

_log = get_logger(__name__)


def create_app(
    settings: ServiceSettings,
    descriptor: ModuleDescriptor,
    *,
    routers: Sequence[APIRouter] = (),
    readiness_checks: Sequence[ReadinessCheck | CheckFunction] = (),
    on_startup: Sequence[LifecycleHook] = (),
    on_shutdown: Sequence[LifecycleHook] = (),
    bus: EventBus | None = None,
    principal_resolver: PrincipalResolver | None = None,
    demo_policy: DemoPolicy | None = None,
) -> FastAPI:
    """Build the FastAPI app of a service.

    Args:
        settings: Service settings (see :class:`ServiceSettings`).
        descriptor: Served at ``/.well-known/mowdeck-module``.
        routers: Mounted under ``/api/v1``.
        readiness_checks: Checks for ``/readyz``; a ``nats`` check is added automatically.
        on_startup: Hooks run in order after the bus is connected.
        on_shutdown: Hooks run in order on shutdown, within ``shutdown_grace_seconds``,
            before the bus is drained and closed.
        bus: Event bus to use. When None and ``settings.nats_enabled`` is true, a
            :class:`JetStreamBus` is connected on startup. A given bus is closed on shutdown.
        principal_resolver: Resolves the principal of a request; defaults to denying all.
        demo_policy: What the service refuses with ``settings.demo_mode`` (ADR 0068); without
            it a demo service only marks its responses.

    State on ``app.state``: ``settings``, ``descriptor``, ``bus``, ``metrics_registry``,
    ``readiness_checks``, ``principal_resolver``, ``permission_checker`` (None until a library
    such as ``mowdeck-authz`` installs one) and ``draining``.
    """
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.draining = False
        active_bus = bus
        if active_bus is None and settings.nats_enabled:
            jetstream = JetStreamBus.from_settings(settings)
            await jetstream.connect()
            active_bus = jetstream
        app.state.bus = active_bus
        if active_bus is not None:
            connected_bus = active_bus

            async def nats() -> bool:
                return connected_bus.is_connected

            app.state.readiness_checks = [c for c in app.state.readiness_checks if c.name != "nats"]
            add_readiness_check(app, ReadinessCheck(name="nats", check=nats))
        watcher: asyncio.Task[None] | None = None
        try:
            for hook in on_startup:
                await hook(app)
            if settings.readiness_interval_seconds > 0:
                watcher = asyncio.create_task(
                    watch_readiness(app, settings.readiness_interval_seconds),
                    name="readiness-watch",
                )
            _log.info(
                "service started",
                version=descriptor.version,
                build=settings.version or "dev",
                source=settings.source_code_url,
            )
            yield
        finally:
            app.state.draining = True
            if watcher is not None:
                watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watcher
            await _shutdown(app, on_shutdown, active_bus, settings.shutdown_grace_seconds)
            if otel_shutdown is not None:
                await otel_shutdown()
            _log.info("service stopped")

    app = FastAPI(
        title=descriptor.name,
        version=descriptor.version,
        lifespan=lifespan,
        openapi_url=f"{API_PREFIX}/openapi.json",
        # No Swagger UI or ReDoc: FastAPI's pages load their bundles from a CDN. The interactive
        # explorer is the web app's, self-hosted (ADR 0072); the reference is docs/reference/api.
        docs_url=None,
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.descriptor = descriptor
    app.state.bus = bus
    app.state.draining = False
    app.state.started = False
    app.state.readiness_checks = [as_readiness_check(c) for c in readiness_checks]
    app.state.principal_resolver = principal_resolver
    app.state.permission_checker = None
    app.state.metrics_registry = None
    app.state.readiness_metrics = None

    install_error_handlers(app)
    route_templates: dict[int, str] = {}
    if settings.metrics_enabled:
        registry = create_metrics_registry()
        app.state.metrics_registry = registry
        app.state.readiness_metrics = ReadinessMetrics(registry)
        app.add_middleware(
            HttpMetricsMiddleware,
            registry=registry,
            skip_paths=("/metrics", "/healthz", "/startupz", "/readyz"),
            route_templates=route_templates,
        )
    # Added last = outermost: correlation id is set before errors are logged and rendered. The
    # source code address (AGPL-3.0 section 13) is on every response, errors included.
    app.add_middleware(UnhandledErrorMiddleware)
    if settings.demo_mode:
        # Inside the correlation id (a refusal carries it), outside the routes and their errors
        # (every response is marked).
        seed = settings.demo_seed_token.get_secret_value() if settings.demo_seed_token else None
        app.add_middleware(DemoModeMiddleware, policy=demo_policy or EMPTY_POLICY, seed_token=seed)
        _log.warning("DEMO MODE: simulated mowers only, unsafe actions are refused")
    app.add_middleware(CorrelationIdMiddleware)
    app.add_middleware(SourceCodeMiddleware, url=settings.source_code_url)
    # nosniff everywhere, no-store on authenticated answers without their own Cache-Control.
    app.add_middleware(HardeningHeadersMiddleware)

    app.include_router(build_health_router(descriptor, metrics=settings.metrics_enabled))
    for router in routers:
        app.include_router(router, prefix=API_PREFIX)
        for route in router.routes:
            template = getattr(route, "path_format", None)
            if template is not None:
                route_templates[id(route)] = f"{API_PREFIX}{template}"

    otel_shutdown = instrument_app(app, settings)
    return app


async def _shutdown(
    app: FastAPI,
    hooks: Sequence[LifecycleHook],
    bus: EventBus | None,
    grace_seconds: float,
) -> None:
    try:
        async with asyncio.timeout(grace_seconds):
            for hook in hooks:
                try:
                    await hook(app)
                except Exception:
                    _log.exception("shutdown hook failed", hook=getattr(hook, "__name__", "?"))
    except TimeoutError:
        _log.error("shutdown hooks exceeded the grace period", grace_seconds=grace_seconds)
    if bus is not None:
        try:
            await bus.close()
        except Exception:
            _log.exception("closing the event bus failed")


async def get_bus(request: Request) -> EventBus:
    """FastAPI dependency returning the app's event bus (503 if the bus is disabled)."""
    bus: EventBus | None = getattr(request.app.state, "bus", None)
    if bus is None:
        raise ServiceUnavailable("the event bus is not enabled for this service")
    return bus


Bus = Annotated[EventBus, Depends(get_bus)]
"""Annotated dependency: ``bus: Bus``."""


def run(
    app: FastAPI,
    settings: ServiceSettings,
    *,
    server_class: "type[uvicorn.Server] | None" = None,
    **uvicorn_options: Any,
) -> None:
    """Serve ``app`` with uvicorn using the kit's logging and graceful shutdown settings.

    Args:
        app: The app from :func:`create_app`.
        settings: Host, port and shutdown grace period.
        server_class: A :class:`uvicorn.Server` subclass, for services that must act before
            uvicorn closes the open connections on shutdown (Realtime closes its WebSockets
            with its own close code first). Default: plain ``uvicorn.run``.
        uvicorn_options: Further ``uvicorn.Config`` options, e.g. ``ws_ping_interval``,
            ``ws_ping_timeout`` or ``ws_max_size`` for WebSocket services.
    """
    import uvicorn

    options: dict[str, Any] = {
        "host": settings.http_host,
        "port": settings.http_port,
        "log_config": None,
        "proxy_headers": True,
        "timeout_graceful_shutdown": int(settings.shutdown_grace_seconds) or None,
        **uvicorn_options,
    }
    if server_class is None:
        uvicorn.run(app, **options)
        return
    server = server_class(uvicorn.Config(app, **options))
    server.run()
    if not server.started:
        raise SystemExit(STARTUP_FAILURE_EXIT_CODE)


STARTUP_FAILURE_EXIT_CODE = 3
"""Exit code when the server did not start (the same as ``uvicorn.run``)."""
