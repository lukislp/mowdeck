"""Entry point of camera-relay: wires the layers into one FastAPI app.

``python -m camera_relay.main`` (the container command) loads the settings from the
environment, builds the app and serves it. Tests call :func:`build_app` with their own settings,
bus, clock and port adapters.

Startup: subscribe the request handlers (``relay.camera.attach|renew|detach``, queue group
``camera-relay``) and start the tick that processes the deadlines. Shutdown: stop the tick, end
every running session (reason ``shutdown``), unsubscribe. Readiness: the bus (kit) and the
request subscriptions.

Until the media plane and the source drivers exist (later steps of ADR 0045) the source port is
:class:`~camera_relay.adapters.sources.NoSourceDriver`: every attach is answered with
``unavailable``. The relay keeps its state in memory and runs as a single replica.
"""

import asyncio
import contextlib
from typing import Any

from fastapi import FastAPI
from mowdeck_contracts.v1 import utc_now
from mowdeck_service_kit import ReadinessCheck, create_app, get_logger, run
from mowdeck_service_kit.bus import EventBus

from camera_relay.adapters.bus.events import BusEventPublisher
from camera_relay.adapters.bus.subscriptions import subscribe_all
from camera_relay.adapters.memory import InMemoryRelayRepository
from camera_relay.adapters.sources import NoMediaPlane, NoSourceDriver
from camera_relay.adapters.tickets import Ed25519TicketSigner
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


def build_app(
    settings: Settings,
    *,
    bus: EventBus | None = None,
    clock: Clock | None = None,
    vendor: VendorSessions | None = None,
    media: MediaPlane | None = None,
    signer: TicketSigner | None = None,
    background_tasks: bool = True,
) -> FastAPI:
    """Build the app. Without overrides it runs without source drivers (``unavailable``)."""
    tasks: list[asyncio.Task[Any]] = []

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

    async def shutdown(app: FastAPI) -> None:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await app.state.relay.shutdown()
        for subscription in app.state.relay_subscriptions or []:
            await subscription.unsubscribe()

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
    app.state.relay = RelayService(
        repository=InMemoryRelayRepository(policy=settings.to_policy()),
        vendor=vendor or NoSourceDriver(),
        media=media or NoMediaPlane(),
        events=BusEventPublisher(lambda: app.state.bus),
        signer=signer or _signer(settings),
        clock=clock or utc_now,
        config=settings.to_config(),
    )
    return app


def main() -> None:
    """Load the settings from the environment and serve the app."""
    settings = Settings.from_env()
    run(build_app(settings), settings)


if __name__ == "__main__":
    main()
