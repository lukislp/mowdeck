"""Entry point of camera-relay: wires the layers into one FastAPI app.

``python -m camera_relay.main`` (the container command) loads the settings from the
environment, builds the app and serves it. Tests call :func:`build_app` with their own settings,
bus, clock and port adapters.

Startup: subscribe the request handlers (``relay.camera.attach|renew|detach``, queue group
``camera-relay``) and start the tick that processes the deadlines. Shutdown: stop the tick, end
every running session (reason ``shutdown``), unsubscribe. Readiness: the bus (kit) and the
request subscriptions.

With ``MOWDECK_RELAY_MEDIAMTX_API_URL`` set, the media plane is wired (ADR 0045 step 4): the
gateway's camera sessions through :class:`~camera_relay.adapters.gateway.GatewayVendorSessions`
with the ``fake`` source driver, MediaMTX's auth callback (``/internal/mediamtx/auth``), the
reconcile loop (every ``MOWDECK_RELAY_RECONCILE_SECONDS``) and, with
``MOWDECK_RELAY_TEST_HOOKS=true``, the freeze hook. With ``MOWDECK_RELAY_AGORA_BRIDGE_URL``
also the ``agora`` source driver (step 7: the optional agora-bridge next to the relay; while it
is not reachable, ``agora`` mowers are ``unsupported_provider``). Without it the source port is
:class:`~camera_relay.adapters.sources.NoSourceDriver`: every attach is answered with
``unavailable``. The relay keeps its state in memory and runs as a single replica.
"""

import asyncio
import contextlib
import secrets
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import FastAPI
from mowdeck_contracts.v1 import utc_now
from mowdeck_service_kit import ReadinessCheck, create_app, get_logger, run
from mowdeck_service_kit.bus import EventBus

from camera_relay.adapters.bus.events import BusEventPublisher
from camera_relay.adapters.bus.subscriptions import subscribe_all
from camera_relay.adapters.gateway import GatewayVendorSessions, SourceDriver, service_principal
from camera_relay.adapters.http.routes import auth_router, hooks_router
from camera_relay.adapters.media.bridge_source import (
    BridgeEndpoint,
    BridgeSourceDriver,
    StdinSpawn,
    spawn_stdin_process,
)
from camera_relay.adapters.media.mediamtx import MediaMtxApi, MediaMtxPlane
from camera_relay.adapters.media.pipeline import FakeSourceDriver, MediaServer, RunningSource
from camera_relay.adapters.media.process import Spawn, spawn_process
from camera_relay.adapters.media.reconcile import Reconciler
from camera_relay.adapters.memory import InMemoryRelayRepository
from camera_relay.adapters.sources import NoMediaPlane, NoSourceDriver
from camera_relay.adapters.tickets import Ed25519TicketSigner
from camera_relay.application.media_auth import MediaAuthorizer, WhepBindings
from camera_relay.application.ports import Clock, MediaPlane, TicketSigner, VendorSessions
from camera_relay.application.service import RelayService
from camera_relay.module import DESCRIPTOR
from camera_relay.settings import Settings

# Named explicitly: under "python -m" the module is called __main__.
_log = get_logger("camera_relay.main")


def _signer(settings: Settings) -> TicketSigner:
    key = settings.relay_ticket_signing_key
    if key is None or not key.get_secret_value().strip():
        _log.warning(
            "MOWDECK_RELAY_TICKET_SIGNING_KEY is not set: tickets are signed with a key of this "
            "process only"
        )
        return Ed25519TicketSigner.generate()
    return Ed25519TicketSigner.from_setting(key.get_secret_value())


def _publisher_secret(settings: Settings) -> str:
    configured = settings.relay_publisher_secret
    if configured is not None and configured.get_secret_value().strip():
        return configured.get_secret_value().strip()
    return secrets.token_urlsafe(32)


@dataclass(slots=True)
class MediaStack:
    """The wired media plane (``app.state.media``)."""

    api: MediaMtxApi
    plane: MediaMtxPlane
    fake: FakeSourceDriver
    agora: BridgeSourceDriver | None
    vendor: GatewayVendorSessions
    reconciler: Reconciler


def _media_stack(
    settings: Settings,
    app: FastAPI,
    bindings: WhepBindings,
    secret: str,
    *,
    spawn: Spawn,
    spawn_stdin: StdinSpawn,
    transport: httpx.AsyncBaseTransport | None,
) -> MediaStack:
    assert settings.relay_mediamtx_api_url is not None  # noqa: S101 - media_enabled
    user = settings.relay_publisher_user
    api = MediaMtxApi(
        settings.relay_mediamtx_api_url, user=user, secret=lambda: secret, transport=transport
    )
    server = MediaServer(settings.relay_mediamtx_rtsp_url, user, lambda: secret)
    vendors: list[GatewayVendorSessions] = []

    def failed(source: RunningSource) -> None:
        vendors[0].report_failure(source)

    fake = FakeSourceDriver(
        server=server,
        ffmpeg=settings.relay_ffmpeg_path,
        font=settings.relay_fake_font,
        on_failure=failed,
        spawn=spawn,
    )
    drivers: list[SourceDriver] = [fake]
    agora = None
    if settings.agora_bridge_url is not None:
        agora = BridgeSourceDriver(
            endpoint=BridgeEndpoint.parse(settings.agora_bridge_url),
            server=server,
            ffmpeg=settings.relay_ffmpeg_path,
            on_failure=failed,
            fetch_session=lambda key: vendors[0].camera_session(key),
            spawn=spawn,
            spawn_stdin=spawn_stdin,
        )
        drivers.append(agora)
    vendor = GatewayVendorSessions(
        lambda: app.state.bus,
        drivers,
        reports=lambda: app.state.relay,
        principal=service_principal(settings.relay_service_subject),
        timeout_seconds=settings.relay_gateway_timeout_seconds,
    )
    vendors.append(vendor)
    reconciler = Reconciler(
        api=api,
        bindings=bindings,
        vendor=vendor,
        reports=lambda: app.state.relay,
        stall_polls=settings.relay_media_stall_polls,
    )
    return MediaStack(api, MediaMtxPlane(api, bindings), fake, agora, vendor, reconciler)


def build_app(
    settings: Settings,
    *,
    bus: EventBus | None = None,
    clock: Clock | None = None,
    vendor: VendorSessions | None = None,
    media: MediaPlane | None = None,
    signer: TicketSigner | None = None,
    background_tasks: bool = True,
    spawn: Spawn = spawn_process,
    spawn_stdin: StdinSpawn = spawn_stdin_process,
    media_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Build the app. Without a media server (``MOWDECK_RELAY_MEDIAMTX_API_URL``) and without
    overrides it runs without source drivers (``unavailable``)."""
    tasks: list[asyncio.Task[Any]] = []
    stack: MediaStack | None = None

    async def ticker(service: RelayService) -> None:
        while True:
            await asyncio.sleep(settings.relay_tick_seconds)
            try:
                await service.tick()
            except Exception:
                _log.exception("relay tick failed")

    async def startup(app: FastAPI) -> None:
        service: RelayService = app.state.relay
        active_bus: EventBus | None = app.state.bus
        app.state.relay_subscriptions = (
            await subscribe_all(active_bus, service) if active_bus is not None else None
        )
        if background_tasks:
            tasks.append(asyncio.create_task(ticker(service), name="camera-relay-tick"))
            if stack is not None:
                tasks.append(
                    asyncio.create_task(
                        stack.reconciler.run(settings.relay_reconcile_seconds),
                        name="camera-relay-reconcile",
                    )
                )

    async def shutdown(app: FastAPI) -> None:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await app.state.relay.shutdown()
        for subscription in app.state.relay_subscriptions or []:
            await subscription.unsubscribe()
        if stack is not None:
            await stack.api.close()

    async def subscriptions_active() -> bool:
        active_bus: EventBus | None = app.state.bus
        return (
            getattr(app.state, "relay_subscriptions", None) is not None
            and active_bus is not None
            and active_bus.is_connected
        )

    app = create_app(
        settings,
        DESCRIPTOR,
        readiness_checks=[ReadinessCheck(name="subscriptions", check=subscriptions_active)],
        on_startup=[startup],
        on_shutdown=[shutdown],
        bus=bus,
    )
    active_signer = signer or _signer(settings)
    active_clock = clock or utc_now
    repository = InMemoryRelayRepository(policy=settings.to_policy())
    bindings = WhepBindings()
    secret = _publisher_secret(settings)
    if settings.media_enabled and vendor is None and media is None:
        stack = _media_stack(
            settings,
            app,
            bindings,
            secret,
            spawn=spawn,
            spawn_stdin=spawn_stdin,
            transport=media_transport,
        )
    app.state.media = stack
    app.state.relay = RelayService(
        repository=repository,
        vendor=vendor or (stack.vendor if stack else NoSourceDriver()),
        media=media or (stack.plane if stack else NoMediaPlane()),
        events=BusEventPublisher(lambda: app.state.bus),
        signer=active_signer,
        clock=active_clock,
        config=settings.to_config(),
    )
    app.include_router(
        auth_router(
            MediaAuthorizer(
                repository=repository,
                signer=active_signer,
                clock=active_clock,
                bindings=bindings,
                publisher_user=settings.relay_publisher_user,
                publisher_secret=lambda: secret,
            )
        )
    )
    if settings.relay_test_hooks and stack is not None:
        app.include_router(hooks_router(stack.fake))
    return app


def main() -> None:
    """Load the settings from the environment and serve the app."""
    settings = Settings.from_env()
    run(build_app(settings), settings)


if __name__ == "__main__":
    main()
