"""Entry point of scheduler: wires the layers into one FastAPI app.

``python -m scheduler.main`` (the container command) loads the settings from the environment,
builds the app and serves it. Tests call :func:`build_app` with their own settings, store,
authorizer, bus, weather provider, clock and principal resolver.

Principals: with ``MOWDECK_AUTH_JWKS_URL`` set, bearer access tokens issued by Identity are
verified locally against its JWKS (``mowdeck-auth``); without it every request is 401.
Authorization: OpenFGA (``MOWDECK_OPENFGA_*``) through ``mowdeck-authz``.

Startup: wire the use cases, subscribe (states, map snapshots, grant changes), start the ticker
and the retention job. Readiness: the bus (kit), the database and OpenFGA.
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast

import httpx
from fastapi import FastAPI
from mowdeck_auth import principal_resolver_from_env
from mowdeck_authz import Authorizer, AuthzSettings, FgaConnection, OpenFgaAuthorizer
from mowdeck_authz.fastapi import install_authz
from mowdeck_authz.writer import SupportsInvalidate
from mowdeck_contracts.v1 import utc_now
from mowdeck_service_kit import PrincipalResolver, create_app, get_logger, run
from mowdeck_service_kit.bus import EventBus, Subscription
from mowdeck_service_kit.db import create_engine, readiness_check_db
from mowdeck_service_kit.entitlements import (
    Entitlements,
    EntitlementsClient,
    client_from_settings,
    install_entitlements,
)

from scheduler.adapters.api.demo import DEMO_POLICY
from scheduler.adapters.api.deps import install_error_mapping
from scheduler.adapters.api.routes import router
from scheduler.adapters.bus.fleet import BusFleetGateway
from scheduler.adapters.bus.publisher import BusEventPublisher
from scheduler.adapters.bus.subscriptions import subscribe_all
from scheduler.adapters.db.store import SqlStore
from scheduler.adapters.metrics import RunMetrics
from scheduler.adapters.weather.open_meteo import OpenMeteoProvider
from scheduler.adapters.weather.static import StaticWeatherProvider
from scheduler.application.ports import Clock, FleetGateway, Store, WeatherProvider
from scheduler.application.services import Services, build_services
from scheduler.module import DESCRIPTOR
from scheduler.settings import Settings

# Named explicitly: under "python -m" the module is called __main__.
_log = get_logger("scheduler.main")


def as_cache(authorizer: Authorizer) -> SupportsInvalidate | None:
    """The authorizer as a decision cache, when it has one (``OpenFgaAuthorizer``, the fake)."""
    if callable(getattr(authorizer, "invalidate", None)) and callable(
        getattr(authorizer, "invalidate_subject", None)
    ):
        return cast(SupportsInvalidate, authorizer)
    return None


async def every(seconds: float, name: str, job: Callable[[], Awaitable[Any]]) -> None:
    """Run ``job`` every ``seconds`` until cancelled; failures are logged, not raised."""
    while True:
        await asyncio.sleep(seconds)
        try:
            await job()
        except Exception:
            _log.exception("background job failed", job=name)


def make_provider(settings: Settings, http: httpx.AsyncClient) -> WeatherProvider:
    """The weather provider named by ``MOWDECK_SCHEDULER_WEATHER_PROVIDER``."""
    if settings.scheduler_weather_provider == "static":
        return StaticWeatherProvider(settings.scheduler_static_weather)
    return OpenMeteoProvider(
        http,
        base_url=settings.scheduler_open_meteo_url,
        timeout_seconds=settings.scheduler_weather_timeout_seconds,
        cache_seconds=settings.scheduler_weather_cache_seconds,
    )


def build_app(
    settings: Settings,
    *,
    bus: EventBus | None = None,
    store: Store | None = None,
    authorizer: Authorizer | None = None,
    principal_resolver: PrincipalResolver | None = None,
    clock: Clock | None = None,
    weather: WeatherProvider | None = None,
    fleet: FleetGateway | None = None,
    http_client: httpx.AsyncClient | None = None,
    background_tasks: bool = True,
    entitlements: Entitlements | None = None,
) -> FastAPI:
    """Build the app. Without overrides it uses PostgreSQL, OpenFGA and the configured weather
    provider from the settings."""
    engine = None
    if store is None:
        if settings.database_url is None:
            msg = "MOWDECK_DATABASE_URL (or _FILE) is required"
            raise ValueError(msg)
        engine = create_engine(settings)
        store = SqlStore(engine)
    connection: FgaConnection | None = None
    if authorizer is None:
        connection = FgaConnection(AuthzSettings.from_env())
    active_clock: Clock = clock or utc_now
    http = http_client or httpx.AsyncClient(
        follow_redirects=False, headers={"User-Agent": "MowDeck-Scheduler"}
    )
    own_http = http_client is None
    provider = weather or make_provider(settings, http)
    tasks: list[asyncio.Task[Any]] = []
    store_ref: Store = store
    plans: Entitlements = (
        entitlements if entitlements is not None else client_from_settings(settings)
    )

    async def startup(app: FastAPI) -> None:
        active_bus: EventBus | None = app.state.bus
        active_authorizer: Authorizer = app.state.authorizer
        services = build_services(
            store=store_ref,
            authorizer=active_authorizer,
            fleet=fleet or BusFleetGateway(active_bus, settings.scheduler_fleet_timeout_seconds),
            provider=provider,
            events=BusEventPublisher(active_bus, producer=settings.service_name),
            config=settings.to_config(),
            clock=active_clock,
            observer=RunMetrics(app.state.metrics_registry),
            entitlements=plans,
        )
        app.state.scheduler = services
        subscriptions: list[Subscription] = []
        if active_bus is not None:
            subscriptions = await subscribe_all(
                active_bus, services.ingest, as_cache(active_authorizer)
            )
        if isinstance(plans, EntitlementsClient):
            plans.attach(active_bus)
            plan_events = await plans.subscribe()
            if plan_events is not None:
                subscriptions.append(plan_events)
        app.state.subscriptions = subscriptions
        _log.info("weather provider", provider=provider.name)
        if background_tasks:
            tasks.extend(start_background_tasks(settings, services))

    async def shutdown(app: FastAPI) -> None:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for subscription in getattr(app.state, "subscriptions", []):
            await subscription.unsubscribe()
        if own_http:
            await http.aclose()
        if connection is not None:
            await connection.close()
        if engine is not None:
            await engine.dispose()

    app = create_app(
        settings,
        DESCRIPTOR,
        routers=[router],
        readiness_checks=[readiness_check_db(engine)] if engine is not None else [],
        on_startup=[startup],
        on_shutdown=[shutdown],
        bus=bus,
        principal_resolver=principal_resolver,
        demo_policy=DEMO_POLICY,
    )
    if authorizer is None:
        assert connection is not None  # noqa: S101 - set above
        authorizer = OpenFgaAuthorizer(connection, registry=app.state.metrics_registry)
    install_authz(app, authorizer)
    install_error_mapping(app)
    install_entitlements(app, plans)
    app.state.weather_provider = provider
    return app


def start_background_tasks(settings: Settings, services: Services) -> list[asyncio.Task[Any]]:
    """The ticker (due schedules, follow-up of started jobs) and the run retention."""

    async def purge() -> None:
        deleted = await services.runner.purge()
        if deleted:
            _log.info("run records purged", count=deleted)

    return [
        asyncio.create_task(
            every(settings.scheduler_tick_seconds, "tick", services.runner.tick),
            name="scheduler-ticker",
        ),
        asyncio.create_task(every(3600, "run-retention", purge), name="scheduler-retention"),
    ]


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
