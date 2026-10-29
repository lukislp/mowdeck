"""Entry point of notifications: wires the layers into one FastAPI app.

``python -m notifications.main`` (the container command) loads the settings from the
environment, builds the app and serves it. Tests call :func:`build_app` with their own settings,
store, authorizer, bus, channels and principal resolver.

Principals: with ``MOWDECK_AUTH_JWKS_URL`` set, bearer access tokens issued by Identity are
verified locally against its JWKS (``mowdeck-auth``); without it every request is 401.
Authorization: OpenFGA (``MOWDECK_OPENFGA_*``) through ``mowdeck-authz``.

Startup: load the message catalog and the VAPID keys, create the channels, wire the use cases,
subscribe (states, mower events, commands, map snapshots, telemetry events, identity events,
grant changes) and start the background tasks: the delivery worker, the offline timer, the
outbox pruning, the outbox watchdog and the directory seed from Fleet. Readiness: the bus
(kit), the subscriptions, the database, OpenFGA and the outbox (no stall: a delivery finished
or nothing was due within ``MOWDECK_NOTIFICATIONS_STALL_SECONDS``).
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import httpx
from fastapi import FastAPI
from mowdeck_auth import principal_resolver_from_env
from mowdeck_authz import Authorizer, AuthzSettings, FgaConnection, OpenFgaAuthorizer
from mowdeck_authz.fastapi import install_authz
from mowdeck_authz.writer import SupportsInvalidate
from mowdeck_contracts.v1 import utc_now
from mowdeck_service_kit import (
    PrincipalResolver,
    configure_logging,
    create_app,
    get_logger,
    run,
)
from mowdeck_service_kit.bus import EventBus, Subscription
from mowdeck_service_kit.db import create_engine, readiness_check_db
from mowdeck_service_kit.entitlements import (
    Entitlements,
    EntitlementsClient,
    client_from_settings,
    install_entitlements,
)
from mowdeck_service_kit.health import ReadinessCheck

from notifications.adapters.api.demo import DEMO_POLICY
from notifications.adapters.api.deps import install_error_mapping
from notifications.adapters.api.routes import router
from notifications.adapters.authz import AuthzViewChecker
from notifications.adapters.bus.errors import BusErrorExplainer
from notifications.adapters.bus.fleet import seed_directory
from notifications.adapters.bus.photo import BusPhotoSource
from notifications.adapters.bus.publisher import BusPublisher
from notifications.adapters.bus.subscriptions import subscribe_all
from notifications.adapters.channels import ChannelContext, ChannelRegistry
from notifications.adapters.channels.vapid import VapidKeys, load_keys, load_or_create
from notifications.adapters.db.store import SqlStore
from notifications.adapters.metrics import DeliveryMetrics
from notifications.application.ports import Clock, Store
from notifications.application.services import Services, build_services
from notifications.domain.push_image import ImageLinks
from notifications.messages import load_catalog
from notifications.module import DESCRIPTOR
from notifications.settings import Settings

# Named explicitly: under "python -m" the module is called __main__.
_log = get_logger("notifications.main")


def resolve_vapid(settings: Settings) -> VapidKeys | None:
    """The VAPID keys from the settings, or the development key file (created when missing)."""
    if settings.notifications_vapid_private_key is not None:
        keys = load_keys(
            settings.notifications_vapid_private_key.get_secret_value(),
            settings.notifications_vapid_public_key,
        )
        _log.info("web push configured", vapid_public_key=keys.public_key)
        return keys
    if settings.notifications_vapid_key_path:
        keys, created = load_or_create(Path(settings.notifications_vapid_key_path))
        _log.warning(
            "development VAPID key file in use; set MOWDECK_NOTIFICATIONS_VAPID_PRIVATE_KEY in "
            "production",
            created=created,
            path=settings.notifications_vapid_key_path,
            vapid_public_key=keys.public_key,
        )
        return keys
    return None


def resolve_image_links(settings: Settings) -> ImageLinks | None:
    """The signer of the push image URLs; None (pushes carry no image) when photos are off or
    there is no public URL or no signing key (the APNs key or ``..._IMAGE_SECRET``)."""
    base, secret = settings.image_base_url, settings.image_secret
    if not settings.notifications_image_enabled or not base or secret is None:
        return None
    return ImageLinks(secret, base, timedelta(seconds=settings.notifications_image_url_ttl_seconds))


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


async def delivery_worker(services: Services, idle_seconds: float) -> None:
    """Send the outbox until cancelled.

    Each round claims what fits into the free send slots and starts the sends in the background.
    When a round claimed nothing the worker pauses until a send finishes or ``idle_seconds``
    passed. A failing round is logged and the loop goes on.
    """
    outbox = services.outbox
    while True:
        try:
            claimed = await outbox.dispatch()
        except Exception:
            _log.exception("delivery round failed")
            claimed = 0
        if claimed == 0:
            await outbox.wait(idle_seconds)


async def outbox_watchdog(services: Services) -> None:
    """Log an error while the outbox makes no progress (``/readyz`` fails at the same time)."""
    stalled = services.outbox.stalled_for()
    if stalled is not None:
        _log.error(
            "outbox stalled: no delivery finished while work was due",
            stalled_seconds=round(stalled.total_seconds()),
            in_flight=services.outbox.in_flight(),
        )


def build_app(
    settings: Settings,
    *,
    bus: EventBus | None = None,
    store: Store | None = None,
    authorizer: Authorizer | None = None,
    principal_resolver: PrincipalResolver | None = None,
    clock: Clock | None = None,
    channels: ChannelRegistry | None = None,
    http_client: httpx.AsyncClient | None = None,
    background_tasks: bool = True,
    seed_delay_seconds: float = 1.0,
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
    active_clock: Clock = clock or utc_now
    configure_logging(settings)  # before the key resolution logs (create_app repeats it)
    vapid = resolve_vapid(settings)
    catalog = load_catalog()
    http = http_client or httpx.AsyncClient(follow_redirects=False)
    own_http = http_client is None
    own_registry = channels is None
    image_links = resolve_image_links(settings)
    registry = channels or ChannelRegistry.build(
        ChannelContext(
            settings=settings, http=http, clock=active_clock, vapid=vapid, image_links=image_links
        ),
        settings.channel_names,
    )
    tasks: list[asyncio.Task[Any]] = []
    store_ref: Store = store
    plans: Entitlements = (
        entitlements if entitlements is not None else client_from_settings(settings)
    )

    async def startup(app: FastAPI) -> None:
        active_bus: EventBus | None = app.state.bus
        active_authorizer: Authorizer = app.state.authorizer
        services = build_services(
            store_ref,
            channels=registry,
            views=AuthzViewChecker(active_authorizer),
            publisher=BusPublisher(active_bus) if active_bus is not None else None,
            catalog=catalog,
            config=settings.to_config(),
            clock=active_clock,
            observer=DeliveryMetrics(app.state.metrics_registry),
            photos=(
                BusPhotoSource(
                    active_bus,
                    timeout_seconds=settings.notifications_photo_timeout_seconds,
                    max_bytes=settings.notifications_image_max_bytes,
                )
                if active_bus is not None and settings.notifications_image_enabled
                else None
            ),
            image_links=image_links,
            errors=BusErrorExplainer(active_bus) if active_bus is not None else None,
        )
        app.state.notifications = services
        app.state.directory_seeded = False
        _log.info("notification channels", channels=registry.names())
        subscriptions: list[Subscription] = []
        if active_bus is not None:
            cache = as_cache(active_authorizer)
            subscriptions = await subscribe_all(
                active_bus, services.watcher, services.accounts, cache
            )
        if isinstance(plans, EntitlementsClient):
            plans.attach(active_bus)
            plan_events = await plans.subscribe()
            if plan_events is not None:
                subscriptions.append(plan_events)
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
        services: Services | None = getattr(app.state, "notifications", None)
        if services is not None:
            await services.fanout.aclose()
            await services.outbox.aclose()
        for subscription in getattr(app.state, "subscriptions", []):
            await subscription.unsubscribe()
        if own_registry:
            await registry.aclose()
        if own_http:
            await http.aclose()
        if connection is not None:
            await connection.close()
        if engine is not None:
            await engine.dispose()

    async def subscriptions_active() -> bool:
        subscriptions = getattr(app.state, "subscriptions", [])
        return bool(subscriptions) and app.state.bus is not None and app.state.bus.is_connected

    async def outbox_progressing() -> bool:
        services: Services | None = getattr(app.state, "notifications", None)
        return services is None or services.outbox.stalled_for() is None

    app = create_app(
        settings,
        DESCRIPTOR,
        routers=[router],
        readiness_checks=[
            *([readiness_check_db(engine)] if engine is not None else []),
            ReadinessCheck(name="subscriptions", check=subscriptions_active),
            ReadinessCheck(name="outbox", check=outbox_progressing),
        ],
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
    app.state.notification_channels = registry
    app.state.message_catalog = catalog
    app.state.vapid_public_key = vapid.public_key if vapid is not None else None
    return app


def start_background_tasks(
    app: FastAPI,
    settings: Settings,
    services: Services,
    bus: EventBus | None,
    seed_delay_seconds: float = 1.0,
) -> list[asyncio.Task[Any]]:
    """The delivery worker, the offline timer, the outbox pruning, the outbox watchdog and the
    directory seed."""

    async def sweep() -> None:
        await services.watcher.sweep_offline()

    async def prune() -> None:
        deleted = await services.outbox.prune()
        if deleted:
            _log.info("finished deliveries pruned", count=deleted)
        images = await services.fanout.prune_images()
        if images:
            _log.info("expired notification photos pruned", count=images)

    tasks: list[asyncio.Task[Any]] = [
        asyncio.create_task(
            delivery_worker(services, settings.notifications_worker_interval_seconds),
            name="notifications-delivery-worker",
        ),
        asyncio.create_task(
            every(settings.notifications_offline_check_seconds, "offline", sweep),
            name="notifications-offline-timer",
        ),
        asyncio.create_task(every(3600, "prune", prune), name="notifications-prune"),
        asyncio.create_task(
            every(60, "outbox watchdog", lambda: outbox_watchdog(services)),
            name="notifications-outbox-watchdog",
        ),
    ]
    if bus is not None:

        def seeded(_count: int) -> None:
            app.state.directory_seeded = True

        tasks.append(
            asyncio.create_task(
                seed_directory(
                    bus,
                    services.watcher,
                    timeout_seconds=settings.notifications_fleet_timeout_seconds,
                    retry_max_seconds=settings.notifications_fleet_retry_max_seconds,
                    on_seeded=seeded,
                    first_delay=seed_delay_seconds,
                ),
                name="notifications-directory-seed",
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
