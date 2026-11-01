"""Entry point of the Planner: wires the layers into one FastAPI app.

``python -m planner.main`` (the container command) loads the settings from the environment,
builds the app and serves it. Tests call :func:`build_app` with their own settings, store,
authorizer, bus, registry, track source and principal resolver.

Principals: with ``MOWDECK_AUTH_JWKS_URL`` set, bearer access tokens issued by Identity are
verified locally against its JWKS (``mowdeck-auth``); without it every request is 401.
Authorization: OpenFGA (``MOWDECK_OPENFGA_*``) through ``mowdeck-authz``. Patterns: every
installed ``mowdeck.patterns`` entry point (``mowdeck-patterns`` and plug-ins).

Startup: wire the use cases, subscribe (device snapshots, grant changes). Readiness: the bus
(kit), the database and OpenFGA.
"""

from collections.abc import Mapping
from typing import cast

from fastapi import FastAPI
from mowdeck_auth import principal_resolver_from_env
from mowdeck_authz import Authorizer, AuthzSettings, FgaConnection, OpenFgaAuthorizer
from mowdeck_authz.fastapi import install_authz
from mowdeck_authz.writer import SupportsInvalidate
from mowdeck_contracts.v1 import utc_now
from mowdeck_patterns import PatternRegistry, default_registry
from mowdeck_service_kit import PrincipalResolver, create_app, get_logger, run
from mowdeck_service_kit.bus import EventBus, Subscription
from mowdeck_service_kit.db import create_engine, readiness_check_db
from mowdeck_service_kit.entitlements import (
    Entitlements,
    EntitlementsClient,
    client_from_settings,
    install_entitlements,
)

from planner.adapters.api.demo_routes import router as demo_router
from planner.adapters.api.deps import install_error_mapping
from planner.adapters.api.routes import router
from planner.adapters.bus.subscriptions import subscribe_all
from planner.adapters.bus.telemetry import BusTrackSource
from planner.adapters.db.store import SqlStore
from planner.application.ports import Clock, Store, TrackSource
from planner.application.services import build_services
from planner.module import DESCRIPTOR
from planner.settings import Settings

# Named explicitly: under "python -m" the module is called __main__.
_log = get_logger("planner.main")


def as_cache(authorizer: Authorizer) -> SupportsInvalidate | None:
    """The authorizer as a decision cache, when it has one (``OpenFgaAuthorizer``, the fake)."""
    if callable(getattr(authorizer, "invalidate", None)) and callable(
        getattr(authorizer, "invalidate_subject", None)
    ):
        return cast(SupportsInvalidate, authorizer)
    return None


def build_app(
    settings: Settings,
    *,
    bus: EventBus | None = None,
    store: Store | None = None,
    authorizer: Authorizer | None = None,
    principal_resolver: PrincipalResolver | None = None,
    registry: PatternRegistry | None = None,
    tracks: TrackSource | None = None,
    clock: Clock | None = None,
    entitlements: Entitlements | None = None,
) -> FastAPI:
    """Build the app. Without overrides it uses PostgreSQL and OpenFGA from the settings."""
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
    store_ref: Store = store
    patterns = registry or default_registry()
    plans: Entitlements = (
        entitlements if entitlements is not None else client_from_settings(settings)
    )

    async def startup(app: FastAPI) -> None:
        active_bus: EventBus | None = app.state.bus
        active_authorizer: Authorizer = app.state.authorizer
        services = build_services(
            store=store_ref,
            authorizer=active_authorizer,
            registry=patterns,
            config=settings.to_config(),
            clock=clock or utc_now,
            tracks=tracks or BusTrackSource(active_bus, settings.planner_track_timeout_seconds),
        )
        app.state.planner = services
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
        _log.info("patterns loaded", patterns=patterns.keys())

    async def shutdown(app: FastAPI) -> None:
        for subscription in getattr(app.state, "subscriptions", []):
            await subscription.unsubscribe()
        if connection is not None:
            await connection.close()
        if engine is not None:
            await engine.dispose()

    app = create_app(
        settings,
        DESCRIPTOR,
        routers=[router, demo_router],
        readiness_checks=[readiness_check_db(engine)] if engine is not None else [],
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
    install_entitlements(app, plans)
    return app


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
