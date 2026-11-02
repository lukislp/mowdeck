"""Entry point of realtime: wires the layers into one FastAPI app.

``python -m realtime.main`` (the container command) loads the settings from the environment,
builds the app and serves it with :class:`~realtime.adapters.server.RealtimeServer` (protocol
pings every ``MOWDECK_REALTIME_PING_SECONDS``, frames up to
``MOWDECK_REALTIME_MAX_MESSAGE_BYTES``). Tests call :func:`build_app` with their own settings,
bus, authorizer and token verifier.

Tokens: with ``MOWDECK_AUTH_JWKS_URL`` set, access tokens are verified locally against
Identity's JWKS (``mowdeck-auth``); without it every socket closes with 4401.
Authorization: ``can_view`` on each mower through OpenFGA (``MOWDECK_OPENFGA_*``).

Startup: the hub exists from the start; subscribe the fan-out, the capability snapshots and the
re-check trigger, start the periodic re-check. Manual driving (ADR 0026) asks Fleet over
``fleet.drive.start``/``fleet.drive.stop`` and publishes the frames on ``command.mower.<id>``.
Shutdown: close every socket with 1001, stop the re-check, unsubscribe,
close OpenFGA. Readiness: the bus (kit) and both fan-out subscriptions.
"""

import asyncio
import contextlib
from typing import Any

from fastapi import FastAPI
from mowdeck_authz import Authorizer, AuthzSettings, FgaConnection, OpenFgaAuthorizer
from mowdeck_authz.fastapi import install_authz
from mowdeck_authz.writer import SupportsInvalidate
from mowdeck_contracts.v1 import utc_now
from mowdeck_service_kit import create_app, get_logger, run
from mowdeck_service_kit.bus import EventBus
from mowdeck_service_kit.entitlements import (
    Entitlements,
    EntitlementsClient,
    client_from_settings,
    install_entitlements,
)
from mowdeck_service_kit.health import ReadinessCheck

from realtime.adapters.api.routes import router
from realtime.adapters.auth import RejectAllTokens, verifier_from_env
from realtime.adapters.authz import AuthzAccessChecker
from realtime.adapters.bus.drive import BusDriveBackend
from realtime.adapters.bus.presence import BusPresencePublisher
from realtime.adapters.bus.subscriptions import RealtimeSubscriptions, subscribe_all
from realtime.adapters.metrics import PrometheusMetrics
from realtime.adapters.server import RealtimeServer
from realtime.application.hub import Hub
from realtime.application.ports import Clock, TokenVerifier
from realtime.module import DESCRIPTOR
from realtime.settings import Settings

# Named explicitly: under "python -m" the module is called __main__.
_log = get_logger("realtime.main")


def _invalidation_cache(authorizer: Authorizer) -> SupportsInvalidate | None:
    if hasattr(authorizer, "invalidate") and hasattr(authorizer, "invalidate_subject"):
        cache: SupportsInvalidate = authorizer  # type: ignore[assignment]
        return cache
    return None


def build_app(
    settings: Settings,
    *,
    bus: EventBus | None = None,
    authorizer: Authorizer | None = None,
    verifier: TokenVerifier | None = None,
    clock: Clock | None = None,
    background_tasks: bool = True,
    entitlements: Entitlements | None = None,
) -> FastAPI:
    """Build the app. Without overrides it uses OpenFGA and the JWKS from the environment."""
    connection: FgaConnection | None = None
    if authorizer is None:
        connection = FgaConnection(AuthzSettings.from_env())
    if verifier is None:
        verifier = verifier_from_env()
    if verifier is None:
        _log.warning("MOWDECK_AUTH_JWKS_URL is not set: every socket is closed with 4401")
        verifier = RejectAllTokens()
    tasks: list[asyncio.Task[Any]] = []
    plans: Entitlements = (
        entitlements if entitlements is not None else client_from_settings(settings)
    )

    async def startup(app: FastAPI) -> None:
        hub: Hub = app.state.hub
        active_bus: EventBus | None = app.state.bus
        app.state.realtime_subscriptions = None
        app.state.plan_subscription = None
        if isinstance(plans, EntitlementsClient):
            plans.attach(active_bus)
            app.state.plan_subscription = await plans.subscribe()
        if active_bus is not None:
            app.state.realtime_subscriptions = await subscribe_all(
                active_bus, hub, _invalidation_cache(app.state.authorizer)
            )
        if background_tasks:
            tasks.append(asyncio.create_task(hub.run_rechecks(), name="realtime-recheck"))
            if hub.presence is not None:
                tasks.append(asyncio.create_task(hub.run_presence(), name="realtime-presence"))

    async def shutdown(app: FastAPI) -> None:
        hub: Hub = app.state.hub
        await hub.close_all(timeout=app.state.close_timeout_seconds)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        subscriptions: RealtimeSubscriptions | None = app.state.realtime_subscriptions
        if subscriptions is not None:
            for subscription in subscriptions.all():
                await subscription.unsubscribe()
        plan_subscription = getattr(app.state, "plan_subscription", None)
        if plan_subscription is not None:
            await plan_subscription.unsubscribe()
        if connection is not None:
            await connection.close()

    async def subscriptions_active() -> bool:
        active_bus: EventBus | None = app.state.bus
        subscriptions = getattr(app.state, "realtime_subscriptions", None)
        return (
            subscriptions is not None
            and subscriptions.telemetry is not None
            and subscriptions.results is not None
            and active_bus is not None
            and active_bus.is_connected
        )

    app = create_app(
        settings,
        DESCRIPTOR,
        routers=[router],
        readiness_checks=[ReadinessCheck(name="subscriptions", check=subscriptions_active)],
        on_startup=[startup],
        on_shutdown=[shutdown],
        bus=bus,
    )
    if authorizer is None:
        assert connection is not None  # noqa: S101 - set above
        authorizer = OpenFgaAuthorizer(connection, registry=app.state.metrics_registry)
    # Readiness stays with the bus: open sockets keep their updates during an OpenFGA outage
    # (re-checks are skipped then), new subscriptions are denied (fail closed).
    install_authz(app, authorizer, readiness=False)
    app.state.hub = Hub(
        settings.to_config(),
        verifier=verifier,
        access=AuthzAccessChecker(authorizer),
        clock=clock or utc_now,
        metrics=PrometheusMetrics(app.state.metrics_registry),
        drive=BusDriveBackend(
            lambda: app.state.bus, timeout=settings.realtime_drive_request_timeout_seconds
        ),
        plans=plans,
        # Demo sandboxes (ADR 0076): the Device-Gateway pauses accounts nobody watches.
        presence=BusPresencePublisher(lambda: app.state.bus),
    )
    install_entitlements(app, plans)
    app.state.allowed_origins = settings.allowed_origins
    app.state.close_timeout_seconds = max(1.0, settings.shutdown_grace_seconds / 2)
    return app


def uvicorn_options(settings: Settings) -> dict[str, Any]:
    """WebSocket options for uvicorn: protocol pings and the maximum frame size."""
    ping = settings.realtime_ping_seconds or None
    return {
        "ws_ping_interval": ping,
        "ws_ping_timeout": ping,
        "ws_max_size": settings.realtime_max_message_bytes,
    }


def main() -> None:
    """Load the settings from the environment and serve the app."""
    settings = Settings.from_env()
    run(build_app(settings), settings, server_class=RealtimeServer, **uvicorn_options(settings))


if __name__ == "__main__":
    main()
