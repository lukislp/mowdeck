"""All use cases of Fleet, wired from their ports."""

from collections.abc import Sequence
from dataclasses import dataclass

from mowdeck_authz import Authorizer, TupleBackend, TupleWriter
from mowdeck_authz.writer import SupportsInvalidate
from mowdeck_contracts.v1 import (
    BridgeScanReply,
    BridgeScanRequest,
    CameraSessionReply,
    CameraSessionRequest,
    CloudDiscoverReply,
    CloudDiscoverRequest,
    CloudProvidersReply,
    FirmwareCheckReply,
    FirmwareCheckRequest,
    PathPreviewGatewayRequest,
    PathPreviewReply,
    PhotoReply,
    PhotoRequest,
)
from mowdeck_service_kit.entitlements import Entitlements

from fleet.adapters.crypto import DisabledCipher
from fleet.application.access import Access
from fleet.application.area_names import AreaNameService
from fleet.application.base_stations import BaseStationService
from fleet.application.camera import CameraService
from fleet.application.commands import CommandService
from fleet.application.context import FleetConfig
from fleet.application.drive import DriveService
from fleet.application.groups import GroupService
from fleet.application.linked_accounts import CredentialVault, LinkedAccountService
from fleet.application.links import LinkService
from fleet.application.mowers import MowerService
from fleet.application.path_preview import PathPreviewService
from fleet.application.ports import (
    BridgeScanGateway,
    CameraGateway,
    CameraRelay,
    Clock,
    CloudDiscovery,
    CommandPublisher,
    EventPublisher,
    FirmwareCheckGateway,
    PathPreviewGateway,
    PhotoGateway,
    SecretCipher,
    Store,
)
from fleet.application.sites import SiteService
from fleet.application.throttle import LoginThrottle
from fleet.application.work import UnitOfWork


@dataclass(frozen=True, slots=True)
class Services:
    """The use cases, created once at startup."""

    sites: SiteService
    groups: GroupService
    mowers: MowerService
    area_names: AreaNameService
    commands: CommandService
    drive: DriveService
    camera: CameraService
    path_preview: PathPreviewService
    linked_accounts: LinkedAccountService
    links: LinkService
    base_stations: BaseStationService
    access: Access
    writer: TupleWriter


def build_services(
    *,
    store: Store,
    authorizer: Authorizer,
    tuple_backend: TupleBackend,
    events: EventPublisher,
    commands: CommandPublisher,
    config: FleetConfig,
    clock: Clock,
    caches: Sequence[SupportsInvalidate] = (),
    entitlements: Entitlements | None = None,
    camera_gateway: CameraGateway | None = None,
    camera_relay: CameraRelay | None = None,
    path_preview_gateway: PathPreviewGateway | None = None,
    cloud_discovery: CloudDiscovery | None = None,
    cipher: SecretCipher | None = None,
    bridge_scan_gateway: BridgeScanGateway | None = None,
    firmware_check_gateway: FirmwareCheckGateway | None = None,
    photo_gateway: PhotoGateway | None = None,
    snapshot_min_interval_seconds: float = 30.0,
) -> Services:
    """Wire the use cases. Tuples are written with ``TupleWriter(owner="fleet")``."""
    writer = TupleWriter(tuple_backend, owner="fleet", caches=caches)
    uow = UnitOfWork(store, writer, events, clock)
    access = Access(authorizer)
    sites = SiteService(uow, store, access, config, clock, entitlements)
    vault = CredentialVault(cipher or DisabledCipher())
    command_service = CommandService(store, access, commands, config, clock, entitlements)
    firmware = firmware_check_gateway or _NoFirmwareCheckGateway()
    return Services(
        sites=sites,
        groups=GroupService(uow, store, access, clock, entitlements),
        mowers=MowerService(uow, store, access, sites, config, clock, entitlements, vault),
        area_names=AreaNameService(uow, store, clock),
        commands=command_service,
        drive=DriveService(store, access, clock),
        camera=CameraService(
            store,
            camera_gateway or _NoCameraGateway(),
            clock,
            entitlements,
            camera_relay,
            photos=photo_gateway or _NoPhotoGateway(),
            snapshot_min_interval_seconds=snapshot_min_interval_seconds,
        ),
        path_preview=PathPreviewService(
            store, path_preview_gateway or _NoPathPreviewGateway(), entitlements
        ),
        linked_accounts=LinkedAccountService(
            uow,
            store,
            access,
            vault,
            cloud_discovery or _NoCloudDiscovery(),
            clock,
            LoginThrottle(clock),
        ),
        links=LinkService(
            uow,
            store,
            vault,
            bridge_scan_gateway or _NoBridgeScanGateway(),
            firmware,
            config,
            clock,
        ),
        base_stations=BaseStationService(
            uow, store, access, vault, firmware, command_service, config, clock
        ),
        access=access,
        writer=writer,
    )


class _NoCameraGateway:
    """Without a bus no gateway can answer (tests and NATS-less runs): every request fails."""

    async def request(self, request: CameraSessionRequest) -> CameraSessionReply:
        msg = "no device gateway is reachable (no event bus)"
        raise ConnectionError(msg)


class _NoPhotoGateway:
    """Without a bus no gateway can take a photo: every request fails."""

    async def request(self, request: PhotoRequest) -> PhotoReply:
        msg = "no device gateway is reachable (no event bus)"
        raise ConnectionError(msg)


class _NoPathPreviewGateway:
    """Without a bus no gateway can plan a route: every request fails."""

    async def request(self, request: PathPreviewGatewayRequest) -> PathPreviewReply:
        msg = "no device gateway is reachable (no event bus)"
        raise ConnectionError(msg)


class _NoBridgeScanGateway:
    """Without a bus no gateway can scan through a bridge: every request fails."""

    async def request(self, request: BridgeScanRequest) -> BridgeScanReply:
        msg = "no device gateway is reachable (no event bus)"
        raise ConnectionError(msg)


class _NoFirmwareCheckGateway:
    """Without a bus no gateway can ask the vendor for firmware: every request fails."""

    async def request(self, request: FirmwareCheckRequest) -> FirmwareCheckReply:
        msg = "no device gateway is reachable (no event bus)"
        raise ConnectionError(msg)


class _NoCloudDiscovery:
    """Without a bus no gateway can log in to a vendor cloud: every request fails."""

    async def discover(self, request: CloudDiscoverRequest) -> CloudDiscoverReply:
        msg = "no device gateway is reachable (no event bus)"
        raise ConnectionError(msg)

    async def providers(self) -> CloudProvidersReply:
        msg = "no device gateway is reachable (no event bus)"
        raise ConnectionError(msg)
