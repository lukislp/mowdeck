"""Entry point of fleet: wires the layers into one FastAPI app.

``python -m fleet.main`` (the container command) loads the settings from the environment,
builds the app and serves it. Tests call :func:`build_app` with their own settings, store,
authorizer, bus and principal resolver.

Principals: with ``MOWDECK_AUTH_JWKS_URL`` set, bearer access tokens issued by Identity are
verified locally against its JWKS (``mowdeck-auth``); without it every request is 401.
Authorization: OpenFGA (``MOWDECK_OPENFGA_*``) through ``mowdeck-authz``.

Plans (ADR 0028): the Entitlements client of the kit (``entitlements.check`` with a snapshot
cache, dropped on ``entitlements.plan.changed``) gates mower, site and group creation and every
command except the emergency stop.

Startup: wire the use cases, subscribe (``fleet.mowers.list``, command results, account
events, cache invalidation), start the retention job. Shutdown: stop the job, unsubscribe,
close OpenFGA and the database.
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from fastapi import FastAPI
from mowdeck_auth import principal_resolver_from_env
from mowdeck_authz import (
    Authorizer,
    AuthzSettings,
    FgaConnection,
    OpenFgaAuthorizer,
    OpenFgaTupleBackend,
    TupleBackend,
)
from mowdeck_authz.fastapi import install_authz
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

from fleet.adapters.api.deps import install_error_mapping
from fleet.adapters.api.routes import ROUTERS
from fleet.adapters.bus.camera import BusCameraGateway
from fleet.adapters.bus.cloud import BusCloudDiscovery
from fleet.adapters.bus.path_preview import BusPathPreviewGateway
from fleet.adapters.bus.publisher import BusCommandPublisher, BusEventPublisher
from fleet.adapters.bus.subscriptions import subscribe_all
from fleet.adapters.crypto import cipher_from_key
from fleet.adapters.db.store import SqlStore
from fleet.application.ports import Clock, Store
from fleet.application.services import Services, build_services
from fleet.module import DESCRIPTOR
from fleet.settings import Settings

# Named explicitly: under "python -m" the module is called __main__.
_log = get_logger("fleet.main")


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
    authorizer: Authorizer | None = None,
    tuple_backend: TupleBackend | None = None,
    principal_resolver: PrincipalResolver | None = None,
    clock: Clock | None = None,
    background_tasks: bool = True,
    entitlements: Entitlements | None = None,
) -> FastAPI:
    """Build the app. Without overrides it uses PostgreSQL and OpenFGA from the settings and
    asks Entitlements over the bus."""
    engine = None
    if store is None:
        if settings.database_url is None:
            msg = "MOWDECK_DATABASE_URL (or _FILE) is required"
            raise ValueError(msg)
        engine = create_engine(settings)
        store = SqlStore(engine)
    connection: FgaConnection | None = None
    if authorizer is None or tuple_backend is None:
        connection = FgaConnection(AuthzSettings.from_env())
        tuple_backend = tuple_backend or OpenFgaTupleBackend(connection)
    active_clock: Clock = clock or utc_now
    tasks: list[asyncio.Task[None]] = []
    store_ref: Store = store
    backend_ref: TupleBackend = tuple_backend
    plans: Entitlements = (
        entitlements if entitlements is not None else client_from_settings(settings)
    )
    key = settings.credential_encryption_key
    previous = settings.credential_encryption_key_previous
    cipher = cipher_from_key(
        key.get_secret_value() if key is not None else None,
        previous.get_secret_value() if previous is not None else None,
    )
    if not cipher.enabled:
        _log.warning(
            "MOWDECK_CREDENTIAL_ENCRYPTION_KEY is not set: linked Mammotion accounts are off"
        )

    async def startup(app: FastAPI) -> None:
        active_bus: EventBus | None = app.state.bus
        authz: Authorizer = app.state.authorizer
        caches: list[Any] = [authz] if hasattr(authz, "invalidate_subject") else []
        services = build_services(
            store=store_ref,
            authorizer=authz,
            tuple_backend=backend_ref,
            events=BusEventPublisher(active_bus, producer=settings.service_name),
            commands=BusCommandPublisher(active_bus),
            config=settings.to_config(),
            clock=active_clock,
            caches=caches,
            entitlements=plans,
            camera_gateway=BusCameraGateway(
                active_bus, timeout_seconds=settings.camera_request_timeout_seconds
            ),
            path_preview_gateway=BusPathPreviewGateway(
                active_bus, timeout_seconds=settings.path_preview_request_timeout_seconds
            ),
            cloud_discovery=BusCloudDiscovery(
                active_bus, timeout_seconds=settings.cloud_discovery_timeout_seconds
            ),
            cipher=cipher,
        )
        app.state.fleet = services
        subscriptions: list[Subscription] = []
        if active_bus is not None:
            subscriptions = await subscribe_all(
                active_bus,
                services,
                caches[0] if caches else None,
                registry=app.state.metrics_registry,
            )
        if isinstance(plans, EntitlementsClient):
            plans.attach(active_bus)
            plan_events = await plans.subscribe()
            if plan_events is not None:
                subscriptions.append(plan_events)
        app.state.subscriptions = subscriptions
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
        if connection is not None:
            await connection.close()
        if engine is not None:
            await engine.dispose()

    app = create_app(
        settings,
        DESCRIPTOR,
        routers=ROUTERS,
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


def start_background_tasks(settings: Settings, services: Services) -> list[asyncio.Task[None]]:
    """The retention job of the command records."""

    async def purge() -> None:
        deleted = await services.commands.purge()
        if deleted:
            _log.info("command records purged", count=deleted)
        sessions = await services.drive.purge(settings.to_config().command_retention)
        if sessions:
            _log.info("drive sessions purged", count=sessions)

    return [
        asyncio.create_task(
            every(settings.command_retention_interval_seconds, "command-retention", purge),
            name="command-retention",
        )
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
