"""Entry point of entitlements: wires the layers into one FastAPI app.

``python -m entitlements.main`` (the container command) loads the settings from the
environment, builds the app and serves it. Tests call :func:`build_app` with their own settings,
store, authorizer, bus, module source, clock and principal resolver.

Startup: seed the plans from the plan file, merge the own feature descriptor, subscribe
(``entitlements.check``, ``identity.account.created``), announce ``entitlements.plan.changed``
for all accounts (settings such as ``MOWDECK_ENTITLEMENTS_BILLING_ENABLED`` may have changed),
then fetch the module descriptors in the background. Readiness: the bus (kit), the database and
OpenFGA (operator endpoints).
"""

import asyncio
import contextlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from mowdeck_auth import principal_resolver_from_env
from mowdeck_authz import Authorizer, AuthzSettings, FgaConnection, OpenFgaAuthorizer
from mowdeck_authz.events import subscribe_cache_invalidation
from mowdeck_authz.fastapi import install_authz
from mowdeck_contracts.v1 import utc_now
from mowdeck_service_kit import PrincipalResolver, create_app, get_logger, run
from mowdeck_service_kit.bus import EventBus, Subscription
from mowdeck_service_kit.db import create_engine, readiness_check_db

from entitlements.adapters.api.demo import DEMO_POLICY
from entitlements.adapters.api.deps import install_error_mapping
from entitlements.adapters.api.routes import router
from entitlements.adapters.bus.publisher import BusEventPublisher
from entitlements.adapters.bus.subscriptions import subscribe_all
from entitlements.adapters.db.store import SqlStore
from entitlements.adapters.modules import HttpModuleSource
from entitlements.application.ports import Clock, ModuleSource, Store
from entitlements.application.services import Services, build_services
from entitlements.domain.planfile import PlanSpec, parse_plan_file
from entitlements.module import DESCRIPTOR
from entitlements.settings import Settings

# Named explicitly: under "python -m" the module is called __main__.
_log = get_logger("entitlements.main")


def load_plan_specs(path: str) -> list[PlanSpec]:
    """The plans of the plan file; a missing file gives none (only ``unlimited`` exists then).

    An invalid file raises ``InvalidInput``: better no start than wrong plans.
    """
    file = Path(path)
    if not path or not file.is_file():
        _log.warning("plan file not found; only the built-in plan unlimited exists", path=path)
        return []
    specs = parse_plan_file(file.read_text(encoding="utf-8"))
    _log.info("plan file read", path=path, plans=[s.key for s in specs])
    return specs


def build_app(
    settings: Settings,
    *,
    bus: EventBus | None = None,
    store: Store | None = None,
    authorizer: Authorizer | None = None,
    principal_resolver: PrincipalResolver | None = None,
    module_source: ModuleSource | None = None,
    clock: Clock | None = None,
    plan_specs: list[PlanSpec] | None = None,
    background_tasks: bool = True,
) -> FastAPI:
    """Build the app. Without overrides it uses PostgreSQL, OpenFGA and the plan file."""
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
    source = module_source
    if source is None and settings.module_urls:
        source = HttpModuleSource(settings.module_urls)
    store_ref: Store = store
    tasks: list[asyncio.Task[Any]] = []

    async def startup(app: FastAPI) -> None:
        active_bus: EventBus | None = app.state.bus
        services = build_services(
            store=store_ref,
            own_descriptor=DESCRIPTOR,
            events=BusEventPublisher(active_bus, producer=settings.service_name),
            config=settings.to_config(),
            clock=clock or utc_now,
            source=source,
        )
        await services.catalogue.refresh(fetch_modules=False)
        specs = (
            plan_specs
            if plan_specs is not None
            else load_plan_specs(settings.entitlements_plans_file)
        )
        written = await services.entitlements.seed(specs)
        _log.info(
            "plans seeded",
            written=written,
            billing_enabled=settings.entitlements_billing_enabled,
            default_plan=settings.entitlements_default_plan,
        )
        app.state.entitlements_services = services
        subscriptions: list[Subscription] = []
        if active_bus is not None:
            subscriptions = await subscribe_all(
                active_bus, services, registry=app.state.metrics_registry
            )
            cache: Any = app.state.authorizer
            if hasattr(cache, "invalidate_subject"):
                # identity.grants.changed: an operator admin granted or revoked takes effect now.
                subscriptions.append(await subscribe_cache_invalidation(active_bus, cache))
        app.state.subscriptions = subscriptions
        await services.entitlements.announce_restart()
        if background_tasks and source is not None:
            tasks.append(
                asyncio.create_task(catalogue_loop(settings, services), name="feature-catalogue")
            )

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
    return app


async def catalogue_loop(settings: Settings, services: Services) -> None:
    """Fetch the module descriptors until cancelled; warn about unknown plan features once
    the catalogue is complete."""
    warned = False
    while True:
        try:
            missing = await services.catalogue.refresh(fetch_modules=True)
            if not missing and not warned:
                await services.entitlements.warn_unknown_features()
                warned = True
        except Exception:
            _log.exception("refreshing the feature catalogue failed")
            missing = ["error"]
        await asyncio.sleep(30.0 if missing else settings.entitlements_catalogue_refresh_seconds)


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
