"""Entry point of device-gateway: wires the layers into one FastAPI app.

``python -m device_gateway.main`` (the container command) loads the settings from the
environment, builds the app and serves it. Tests call :func:`build_app` with their own settings,
bus, principal resolver and connection provider.

Principals: with ``MOWDECK_AUTH_JWKS_URL`` set, bearer access tokens issued by Identity are
verified locally against its JWKS (``mowdeck-auth``); without it every request is 401.

Start-up order: registry events, bootstrap mowers, command consumer, camera session and release
requests, path preview requests, bridge scan and firmware check requests, cloud discovery
requests, Fleet sync (background).
Shutdown order: stop the command consumer, answer in-flight commands, close every connection
(publishing ``mower.disconnected``), stop the shared cloud sessions; the kit then drains and
closes the bus.
"""

from collections.abc import Mapping

from fastapi import FastAPI
from mowdeck_auth import principal_resolver_from_env
from mowdeck_service_kit import (
    PrincipalResolver,
    ReadinessCheck,
    add_readiness_check,
    create_app,
    get_logger,
    run,
)
from mowdeck_service_kit.bus import EventBus, Subscription

from device_gateway.adapters.api.routes import router
from device_gateway.adapters.bus.bridge import subscribe_bridge_scans
from device_gateway.adapters.bus.camera import subscribe_camera_releases, subscribe_camera_requests
from device_gateway.adapters.bus.cloud import DiscoveryMetrics, subscribe_cloud_discovery
from device_gateway.adapters.bus.firmware import subscribe_firmware_checks
from device_gateway.adapters.bus.fleet import BusCredentialSource, BusFleetDirectory
from device_gateway.adapters.bus.path_preview import subscribe_path_previews
from device_gateway.adapters.bus.publisher import BusPublisher
from device_gateway.adapters.bus.subscriptions import (
    CommandSubscription,
    subscribe_photo_requests,
    subscribe_registry_events,
)
from device_gateway.adapters.bus.tracking import subscribe_tracking_leases
from device_gateway.adapters.control.cloud_accounts import CloudBackend, cloud_backend
from device_gateway.adapters.control.connections import RegistryConnectionProvider
from device_gateway.adapters.control.snapshot import RelaySnapshotter
from device_gateway.application.discovery import CloudDiscoveryService
from device_gateway.application.gateway import DeviceGateway
from device_gateway.application.ports import (
    CameraSnapshotter,
    CloudAccounts,
    ConnectionProvider,
    FleetDirectory,
)
from device_gateway.application.session import SessionTiming
from device_gateway.domain.rules import Shard
from device_gateway.module import DESCRIPTOR
from device_gateway.settings import Settings

# Named explicitly: under "python -m" the module is called __main__.
_log = get_logger("device_gateway.main")


def relay_snapshotter_from(settings: Settings) -> CameraSnapshotter | None:
    """The camera relay's frame tap when ``MOWDECK_RELAY_URL`` and its token are set."""
    token = (
        settings.relay_frames_token.get_secret_value().strip()
        if settings.relay_frames_token
        else ""
    )
    if not settings.relay_url:
        return None
    if not token:
        _log.warning("MOWDECK_RELAY_URL is set without MOWDECK_RELAY_FRAMES_TOKEN: no relay frames")
        return None
    _log.info("camera frames through the camera relay", url=settings.relay_url)
    return RelaySnapshotter(settings.relay_url, token=token, max_width=settings.capture_max_width)


def build_app(
    settings: Settings,
    *,
    bus: EventBus | None = None,
    principal_resolver: PrincipalResolver | None = None,
    connection_provider: ConnectionProvider | None = None,
    fleet: FleetDirectory | None = None,
    cloud: CloudBackend | None = None,
    cloud_accounts: CloudAccounts | None = None,
    relay_snapshotter: CameraSnapshotter | None = None,
) -> FastAPI:
    """Build the app. ``bus`` and ``principal_resolver`` default to the kit's behaviour;
    ``connection_provider`` and ``fleet`` default to the control registry and the bus;
    ``cloud`` to the real Mammotion cloud (the fake one with ``fake_mammotion_cloud``) and
    ``cloud_accounts`` to its discovery."""
    backend = cloud or cloud_backend(
        fake=settings.fake_mammotion_cloud, cache_dir=settings.cloud_session_cache_dir
    )
    if backend.fake:
        _log.warning(
            "using the FAKE Mammotion cloud (MOWDECK_FAKE_MAMMOTION_CLOUD is for development only)"
        )
    # Public demo (ADR 0068): simulated mowers only, no manufacturer cloud at all.
    demo = settings.demo_mode
    provider = connection_provider or RegistryConnectionProvider(
        sim_time_scale=settings.sim_time_scale,
        sim_tick_seconds=settings.sim_tick_seconds,
        cloud_factory=None if demo else backend.factory,
        simulated_only=demo,
    )
    accounts: CloudAccounts = cloud_accounts or backend.accounts
    # Photos of mowers without an own photo function come from the camera relay (ADR 0045).
    relay = relay_snapshotter or relay_snapshotter_from(settings)
    frames = (relay,) if relay is not None else ()

    async def startup(app: FastAPI) -> None:
        active_bus: EventBus | None = app.state.bus
        if active_bus is None:
            msg = "the device gateway needs the event bus (MOWDECK_NATS_ENABLED=true)"
            raise RuntimeError(msg)
        gateway = DeviceGateway(
            shard=Shard(settings.shard_index, settings.shard_count),
            provider=provider,
            publisher=BusPublisher(active_bus, producer=settings.service_name),
            timing=SessionTiming(
                heartbeat_seconds=settings.telemetry_heartbeat_seconds,
                drive_heartbeat_seconds=settings.telemetry_drive_heartbeat_seconds,
                snapshot_recheck_seconds=settings.snapshot_recheck_seconds,
                reconnect_initial_seconds=settings.reconnect_initial_seconds,
                reconnect_max_seconds=settings.reconnect_max_seconds,
                connection_check_seconds=settings.connection_check_seconds,
                path_progress_seconds=settings.path_progress_seconds,
                firmware_check_initial_seconds=settings.firmware_check_initial_seconds,
                firmware_check_interval_seconds=settings.firmware_check_interval_seconds,
            ),
            credentials=BusCredentialSource(
                active_bus, timeout_seconds=settings.fleet_request_timeout_seconds
            ),
            snapshotters=frames,
            snapshot_timeout_seconds=settings.capture_timeout_seconds,
        )
        app.state.gateway = gateway
        registry_events: Subscription = await subscribe_registry_events(active_bus, gateway)
        app.state.registry_events = registry_events
        app.state.photo_requests = await subscribe_photo_requests(
            active_bus, gateway, timeout_seconds=settings.photo_timeout_seconds
        )
        if settings.bootstrap_mowers:
            _log.warning(
                "registering bootstrap mowers (MOWDECK_BOOTSTRAP_MOWERS is for development only)",
                count=len(settings.bootstrap_mowers),
            )
            await gateway.register_all(settings.bootstrap_mowers, source="bootstrap")
        commands = CommandSubscription(active_bus, gateway, durable=settings.command_consumer)
        app.state.commands = commands
        await commands.start()

        async def command_consumer() -> bool:
            return commands.active and gateway.accepting

        add_readiness_check(app, ReadinessCheck(name="commands", check=command_consumer))
        app.state.camera_requests = await subscribe_camera_requests(
            active_bus, gateway, registry=app.state.metrics_registry
        )
        app.state.camera_releases = await subscribe_camera_releases(
            active_bus, gateway, registry=app.state.metrics_registry
        )
        app.state.path_previews = await subscribe_path_previews(
            active_bus, gateway, timeout_seconds=settings.path_preview_timeout_seconds
        )
        app.state.bridge_scans = await subscribe_bridge_scans(
            active_bus, gateway, registry=app.state.metrics_registry
        )
        app.state.firmware_checks = await subscribe_firmware_checks(
            active_bus, gateway, registry=app.state.metrics_registry
        )
        app.state.tracking_leases = await subscribe_tracking_leases(
            active_bus, gateway, registry=app.state.metrics_registry
        )
        if not demo:  # a demo never logs in to a manufacturer cloud
            app.state.cloud_discovery = await subscribe_cloud_discovery(
                active_bus,
                CloudDiscoveryService(
                    accounts,
                    timeout_seconds=settings.cloud_discovery_timeout_seconds,
                    count=DiscoveryMetrics(app.state.metrics_registry).count,
                    concurrency=settings.cloud_discovery_concurrency,
                ),
            )
        gateway.start_fleet_sync(
            fleet
            or BusFleetDirectory(
                active_bus, timeout_seconds=settings.fleet_request_timeout_seconds
            ),
            retry_max_seconds=settings.fleet_retry_max_seconds,
        )
        _log.info(
            "device gateway ready",
            shard_index=settings.shard_index,
            shard_count=settings.shard_count,
            consumer=settings.command_consumer,
        )

    async def shutdown(app: FastAPI) -> None:
        commands: CommandSubscription | None = getattr(app.state, "commands", None)
        gateway: DeviceGateway | None = getattr(app.state, "gateway", None)
        if gateway is not None:
            gateway.stop_accepting()
        if commands is not None:
            await commands.stop()
        camera_requests: Subscription | None = getattr(app.state, "camera_requests", None)
        if camera_requests is not None:
            await camera_requests.unsubscribe()
        path_previews: Subscription | None = getattr(app.state, "path_previews", None)
        if path_previews is not None:
            await path_previews.unsubscribe()
        for name in ("camera_releases", "bridge_scans", "firmware_checks", "tracking_leases"):
            subscription: Subscription | None = getattr(app.state, name, None)
            if subscription is not None:
                await subscription.unsubscribe()
        cloud_discovery: Subscription | None = getattr(app.state, "cloud_discovery", None)
        if cloud_discovery is not None:
            await cloud_discovery.unsubscribe()
        registry_events: Subscription | None = getattr(app.state, "registry_events", None)
        if registry_events is not None:
            await registry_events.unsubscribe()
        photo_requests: Subscription | None = getattr(app.state, "photo_requests", None)
        if photo_requests is not None:
            await photo_requests.unsubscribe()
        if gateway is not None:
            await gateway.close()
        await backend.pool.close()

    return create_app(
        settings,
        DESCRIPTOR,
        routers=[router],
        on_startup=[startup],
        on_shutdown=[shutdown],
        bus=bus,
        principal_resolver=principal_resolver,
    )


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
