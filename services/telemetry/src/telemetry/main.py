"""Entry point of telemetry: wires the layers into one FastAPI app.

``python -m telemetry.main`` (the container command) loads the settings from the environment,
builds the app and serves it. Tests call :func:`build_app` with their own settings, store,
authorizer, bus and principal resolver.

Principals: with ``MOWDECK_AUTH_JWKS_URL`` set, bearer access tokens issued by Identity are
verified locally against its JWKS (``mowdeck-auth``); without it every request is 401.
Authorization: OpenFGA (``MOWDECK_OPENFGA_*``) through ``mowdeck-authz``.

Startup: wire the use cases, subscribe (states, command results, mower events), seed the mower
directory from Fleet in the background and start the retention job. Shutdown: stop the tasks,
unsubscribe, close OpenFGA and the database. Readiness: the bus (kit), the subscriptions, the
database and OpenFGA.
"""

import asyncio
import contextlib
import dataclasses
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from mowdeck_auth import principal_resolver_from_env
from mowdeck_authz import Authorizer, AuthzSettings, FgaConnection, OpenFgaAuthorizer
from mowdeck_authz.fastapi import install_authz
from mowdeck_contracts.v1 import utc_now
from mowdeck_service_kit import PrincipalResolver, create_app, get_logger, run
from mowdeck_service_kit.bus import EventBus, Subscription
from mowdeck_service_kit.db import create_engine, readiness_check_db
from mowdeck_service_kit.health import ReadinessCheck

from telemetry.adapters.aerial import DiskTileCache, HttpTileSource
from telemetry.adapters.api.deps import install_error_mapping
from telemetry.adapters.api.routes import router
from telemetry.adapters.api.tracking_routes import router as tracking_router
from telemetry.adapters.bus.fleet import seed_directory
from telemetry.adapters.bus.health_events import BusHealthEvents
from telemetry.adapters.bus.subscriptions import subscribe_all
from telemetry.adapters.bus.tracking import (
    BusTrackingEvents,
    BusTrackingGateway,
    subscribe_notifications,
)
from telemetry.adapters.db.store import SqlStore
from telemetry.adapters.db.tracking_store import SqlTrackingStore
from telemetry.adapters.memory_tracking import MemoryTrackingStore
from telemetry.adapters.metrics import PrometheusRetentionObserver
from telemetry.application.aerial import TileCache, TileSource
from telemetry.application.ports import Clock, Store
from telemetry.application.services import Services, build_services
from telemetry.application.tracking import Tracking
from telemetry.application.tracking_map import BaseLayer, TrackingMap, street_layer
from telemetry.application.tracking_ports import TrackingGateway, TrackingStore
from telemetry.module import DESCRIPTOR
from telemetry.settings import Settings

# Named explicitly: under "python -m" the module is called __main__.
_log = get_logger("telemetry.main")


async def every(seconds: float, name: str, job: Callable[[], Awaitable[Any]]) -> None:
    """Run ``job`` every ``seconds`` until cancelled; failures are logged, not raised."""
    while True:
        await asyncio.sleep(seconds)
        try:
            await job()
        except Exception:
            _log.exception("background job failed", job=name)


def build_app(
    settings: Settings,
    *,
    bus: EventBus | None = None,
    store: Store | None = None,
    tracking_store: TrackingStore | None = None,
    tracking_gateway: TrackingGateway | None = None,
    authorizer: Authorizer | None = None,
    principal_resolver: PrincipalResolver | None = None,
    clock: Clock | None = None,
    background_tasks: bool = True,
    seed_delay_seconds: float = 1.0,
    aerial_source: TileSource | None = None,
    aerial_cache: TileCache | None = None,
) -> FastAPI:
    """Build the app. Without overrides it uses PostgreSQL and OpenFGA from the settings."""
    engine = None
    if store is None:
        if settings.database_url is None:
            msg = "MOWDECK_DATABASE_URL (or _FILE) is required"
            raise ValueError(msg)
        engine = create_engine(settings)
        store = SqlStore(engine)
        tracking_store = tracking_store or SqlTrackingStore(engine)
    connection: FgaConnection | None = None
    if authorizer is None:
        connection = FgaConnection(AuthzSettings.from_env())
    active_clock: Clock = clock or utc_now
    if tracking_store is None:
        tracking_store = MemoryTrackingStore()  # only reached with an injected store (tests)
    tasks: list[asyncio.Task[Any]] = []
    store_ref: Store = store
    aerial_config = settings.to_aerial_config()
    http_source: HttpTileSource | None = None
    basemap_on = settings.telemetry_basemap_url is not None
    if aerial_source is None and (aerial_config.enabled or basemap_on):
        http_source = HttpTileSource(
            settings.telemetry_aerial_timeout_seconds, version=settings.version
        )
        aerial_source = http_source
        if aerial_cache is None:
            aerial_cache = DiskTileCache(
                Path(settings.telemetry_aerial_cache_dir),
                settings.telemetry_aerial_cache_max_mb * 1_048_576,
            )
        if aerial_config.enabled:
            _log.info(
                "aerial photo layer on", wms=aerial_config.wms_url, xyz=bool(aerial_config.xyz_url)
            )
        if basemap_on:
            _log.info("tracking street map on")

    async def startup(app: FastAPI) -> None:
        active_bus: EventBus | None = app.state.bus
        events = BusHealthEvents(lambda: getattr(app.state, "bus", None))
        registry = getattr(app.state, "metrics_registry", None)
        observer = PrometheusRetentionObserver(registry) if registry is not None else None
        services = build_services(
            store_ref,
            settings.to_config(),
            active_clock,
            events,
            aerial_config=aerial_config,
            aerial_source=aerial_source,
            aerial_cache=aerial_cache,
            retention_observer=observer,
        )
        tracking = (
            Tracking(
                tracking_store,
                store_ref,
                settings.to_tracking_config(),
                active_clock,
                BusTrackingEvents(lambda: getattr(app.state, "bus", None)),
                tracking_gateway
                or BusTrackingGateway(
                    lambda: getattr(app.state, "bus", None),
                    photo_timeout_seconds=settings.to_tracking_config().photo_timeout_seconds,
                ),
                basemap=tracking_basemap(settings, services, aerial_source, aerial_cache),
            )
            if tracking_store is not None
            else None
        )
        services = dataclasses.replace(services, tracking=tracking)
        app.state.telemetry = services
        app.state.directory_seeded = False
        subscriptions: list[Subscription] = []
        if active_bus is not None:
            subscriptions = await subscribe_all(
                active_bus,
                services.ingest,
                unknown_mower_attempts=settings.telemetry_unknown_mower_attempts,
                queries=services.queries,
            )
        if active_bus is not None and tracking is not None:
            subscriptions.append(await subscribe_notifications(active_bus, tracking))
        app.state.subscriptions = subscriptions
        if background_tasks:
            tasks.extend(
                start_background_tasks(app, settings, services, active_bus, seed_delay_seconds)
            )

    async def shutdown(app: FastAPI) -> None:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        tracking_uc = getattr(getattr(app.state, "telemetry", None), "tracking", None)
        if tracking_uc is not None:
            await tracking_uc.close()
        for subscription in getattr(app.state, "subscriptions", []):
            await subscription.unsubscribe()
        if http_source is not None:
            await http_source.close()
        if connection is not None:
            await connection.close()
        if engine is not None:
            await engine.dispose()

    async def subscriptions_active() -> bool:
        subscriptions = getattr(app.state, "subscriptions", [])
        return bool(subscriptions) and app.state.bus is not None and app.state.bus.is_connected

    app = create_app(
        settings,
        DESCRIPTOR,
        routers=[router, tracking_router],
        readiness_checks=[
            *([readiness_check_db(engine)] if engine is not None else []),
            ReadinessCheck(name="subscriptions", check=subscriptions_active),
        ],
        on_startup=[startup],
        on_shutdown=[shutdown],
        bus=bus,
        principal_resolver=principal_resolver,
    )
    if authorizer is None:
        assert connection is not None  # noqa: S101 - set above
        authorizer = OpenFgaAuthorizer(connection, registry=app.state.metrics_registry)
    install_authz(app, authorizer)
    install_error_mapping(app)
    return app


def tracking_basemap(
    settings: Settings,
    services: Services,
    source: TileSource | None,
    cache: TileCache | None,
) -> TrackingMap | None:
    """The base map under tracking tracks: the street map (``MOWDECK_TELEMETRY_BASEMAP_*``) and
    the aerial photo source, whichever is configured; None without both (ADR 0058)."""
    layers: list[BaseLayer] = []
    if settings.telemetry_basemap_url:
        layers.append(
            street_layer(
                settings.telemetry_basemap_url,
                settings.telemetry_basemap_attribution,
                settings.telemetry_basemap_min_zoom,
                settings.telemetry_basemap_max_zoom,
                coverage=settings.coverage_of(settings.telemetry_basemap_coverage),
            )
        )
    aerial = settings.to_aerial_config()
    if aerial.enabled:
        layers.append(
            BaseLayer(
                id="aerial",
                attribution=aerial.attribution,
                min_zoom=aerial.min_zoom,
                max_zoom=aerial.max_zoom,
                url=services.aerial.upstream_url,
                coverage=settings.coverage_of(settings.telemetry_aerial_coverage),
            )
        )
    if not layers or source is None:
        return None
    return TrackingMap(layers, source, cache, settings.to_tracking_map_config())


def start_background_tasks(
    app: FastAPI,
    settings: Settings,
    services: Services,
    bus: EventBus | None,
    seed_delay_seconds: float = 1.0,
) -> list[asyncio.Task[Any]]:
    """The retention job and the directory seed."""

    async def retain() -> None:
        deleted = await services.retention.run_once()
        if deleted is not None and any(deleted.values()):
            _log.info("retention deleted rows", **{t.value: n for t, n in deleted.items()})
        report = await services.retention.thin_once()
        if report is not None and (report.slices or not report.complete):
            _log.info(
                "position thinning round",
                mowers=report.mowers,
                slices=report.slices,
                scanned=report.scanned,
                deleted=report.deleted,
                seconds=round(report.seconds, 1),
                complete=report.complete,
            )

    tasks: list[asyncio.Task[Any]] = [
        asyncio.create_task(
            every(settings.telemetry_retention_interval_seconds, "retention", retain),
            name="telemetry-retention",
        )
    ]
    tracking = services.tracking
    if tracking is not None:

        async def retain_tracking() -> None:
            deleted = await tracking.retain()
            if deleted:
                _log.info("retention deleted tracking sessions", count=deleted)

        tasks.append(
            asyncio.create_task(
                every(
                    settings.telemetry_retention_interval_seconds,
                    "tracking-retention",
                    retain_tracking,
                ),
                name="telemetry-tracking-retention",
            )
        )
        tasks.append(
            asyncio.create_task(
                every(settings.telemetry_tracking_tick_seconds, "tracking-tick", tracking.tick),
                name="telemetry-tracking-tick",
            )
        )
    if bus is not None:

        def seeded(_count: int) -> None:
            app.state.directory_seeded = True

        tasks.append(
            asyncio.create_task(
                seed_directory(
                    bus,
                    services.ingest,
                    timeout_seconds=settings.telemetry_fleet_timeout_seconds,
                    retry_max_seconds=settings.telemetry_fleet_retry_max_seconds,
                    on_seeded=seeded,
                    first_delay=seed_delay_seconds,
                ),
                name="telemetry-directory-seed",
            )
        )
    return tasks


def default_principal_resolver(
    environ: Mapping[str, str] | None = None,
) -> PrincipalResolver | None:
    """The JWT resolver when ``MOWDECK_AUTH_JWKS_URL`` is set, else None (the kit denies all)."""
    resolver = principal_resolver_from_env(environ)
    if resolver is None:
        _log.warning("MOWDECK_AUTH_JWKS_URL is not set: every API request is answered with 401")
    return resolver


def main() -> None:
    """Load the settings from the environment and serve the app."""
    settings = Settings.from_env()
    run(build_app(settings, principal_resolver=default_principal_resolver()), settings)


if __name__ == "__main__":
    main()
