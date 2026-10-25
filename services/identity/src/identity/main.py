"""Entry point of identity: wires the layers into one FastAPI app.

``python -m identity.main`` (the container command) loads the settings from the environment,
builds the app and serves it. Tests call :func:`build_app` with their own settings, store,
authorizer, bus and password hasher.

Startup: load or create the signing keys, merge the permission catalogue (own descriptor and the
model), wire the use cases, then start the background tasks (module descriptors, key rotation,
reconcile). Shutdown: stop the tasks, close OpenFGA and the database.
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI
from mowdeck_auth import JwtPrincipalResolver, JwtVerifier
from mowdeck_authz import Authorizer, TupleBackend
from mowdeck_authz.fastapi import install_authz
from mowdeck_contracts.v1 import utc_now
from mowdeck_service_kit import (
    PrincipalResolver,
    ReadinessCheck,
    add_readiness_check,
    create_app,
    get_logger,
    run,
)
from mowdeck_service_kit.bus import EventBus
from mowdeck_service_kit.db import readiness_check_db
from mowdeck_service_kit.entitlements import Entitlements, EntitlementsClient

from identity.adapters.api.deps import install_error_mapping
from identity.adapters.api.routes import ROUTERS, jwks_document
from identity.adapters.bus.subscriptions import subscribe_all
from identity.application.ports import Clock, Lt, ModuleSource, PasswordHasher, Store
from identity.module import DESCRIPTOR
from identity.runtime import Runtime
from identity.settings import Settings

# Named explicitly: under "python -m" the module is called __main__.
_log = get_logger("identity.main")


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
    module_source: ModuleSource | None = None,
    hasher: PasswordHasher | None = None,
    clock: Clock | None = None,
    principal_resolver: PrincipalResolver | None = None,
    background_tasks: bool = True,
    entitlements: Entitlements | None = None,
) -> FastAPI:
    """Build the app. Without overrides it uses PostgreSQL, OpenFGA and Argon2id."""
    runtime = Runtime(
        settings,
        store=store,
        authorizer=authorizer,
        tuple_backend=tuple_backend,
        module_source=module_source,
        hasher=hasher,
        clock=clock or utc_now,
        entitlements=entitlements,
    )
    keys = runtime.keys
    verifier = JwtVerifier(
        keys, issuer=settings.identity_issuer, audience=settings.identity_audience
    )
    tasks: list[asyncio.Task[None]] = []

    async def startup(app: FastAPI) -> None:
        active_bus: EventBus | None = app.state.bus
        services = await runtime.start(active_bus)
        app.state.identity = services

        async def signing_key() -> bool:
            keys.active_key()
            return True

        add_readiness_check(app, ReadinessCheck("signing_key", signing_key))
        authz: Any = services.deps.authorizer
        cache = authz if hasattr(authz, "invalidate_subject") else None
        app.state.subscriptions = await subscribe_all(active_bus, cache) if active_bus else []
        if isinstance(runtime.entitlements, EntitlementsClient):
            plan_events = await runtime.entitlements.subscribe()
            if plan_events is not None:
                app.state.subscriptions.append(plan_events)
        if background_tasks:
            tasks.extend(start_background_tasks(settings, runtime))

    async def shutdown(app: FastAPI) -> None:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for subscription in getattr(app.state, "subscriptions", []):
            await subscription.unsubscribe()
        await runtime.close()

    app = create_app(
        settings,
        DESCRIPTOR,
        routers=ROUTERS,
        readiness_checks=[readiness_check_db(runtime.engine)] if runtime.engine else [],
        on_startup=[startup],
        on_shutdown=[shutdown],
        bus=bus,
        principal_resolver=principal_resolver or JwtPrincipalResolver(verifier),
    )
    install_authz(app, runtime.ensure_authorizer(app.state.metrics_registry), token_prefilter=True)
    install_error_mapping(app)
    app.state.trust_forwarded_for = settings.identity_trust_forwarded_for
    app.state.refresh_cookie_secure = settings.identity_refresh_cookie_secure
    app.state.runtime = runtime
    app.add_api_route(
        "/.well-known/jwks.json", jwks_document, methods=["GET"], include_in_schema=False
    )
    return app


def start_background_tasks(settings: Settings, runtime: Runtime) -> list[asyncio.Task[None]]:
    """Module descriptors, key rotation and purges, periodic reconcile."""
    services, store = runtime.services, runtime.store
    assert services is not None and store is not None  # noqa: S101 - called after start()
    keys, catalogue = runtime.keys, runtime.catalogue
    retention = services.deps.config.login_attempt_retention

    async def catalogue_loop() -> None:
        while True:
            try:
                missing = await catalogue.refresh(fetch_modules=True)
            except Exception:
                _log.exception("refreshing the permission catalogue failed")
                missing = ["error"]
            await asyncio.sleep(30.0 if missing else settings.identity_catalogue_refresh_seconds)

    async def maintenance() -> None:
        done = await keys.rotate_step()
        if done:
            _log.info("signing keys rotated", actions=done)
        await keys.load()
        now = runtime.clock()
        async with store.transaction() as tx:
            await tx.login_attempts.delete(at=Lt(now - retention))
            # Passkey ceremonies and app login codes nobody finished (ADR 0050).
            await tx.webauthn_challenges.delete(expires_at=Lt(now))
            await tx.app_login_codes.delete(expires_at=Lt(now))
            await tx.app_login_requests.delete(expires_at=Lt(now))

    tasks = [
        asyncio.create_task(catalogue_loop(), name="catalogue"),
        asyncio.create_task(
            every(settings.identity_maintenance_seconds, "maintenance", maintenance),
            name="maintenance",
        ),
    ]
    if settings.identity_reconcile_seconds > 0:
        reconcile = services.accounts.reconcile_all
        tasks.append(
            asyncio.create_task(
                every(settings.identity_reconcile_seconds, "reconcile", reconcile),
                name="reconcile",
            )
        )
    return tasks


def main() -> None:
    """Load the settings from the environment and serve the app."""
    settings = Settings.from_env()
    run(build_app(settings), settings)


if __name__ == "__main__":
    main()
