"""RTK base station (ADR 0047) in the Device-Gateway against the simulator: the registration's
base station reaches the connection, the state carries it, firmware checks with the target
``base_station`` and the base station update command."""

import asyncio
from typing import Any
from uuid import uuid4

from dg_support import (
    SlowMower,
    TestProvider,
    envelope,
    make_app,
    registration,
    telemetry,
    wait_until,
)
from mowdeck_contracts.v1 import (
    BASE_STATION_OPTION,
    BaseStationFirmwareUpdate,
    BaseStationRef,
    CommandStatus,
    FirmwareCheckErrorCode,
    FirmwareCheckReply,
    FirmwareCheckRequest,
    FirmwareInfo,
    MowerRegistration,
    RtkCorrectionLink,
)
from mowdeck_contracts.v1.subjects import firmware_check_subject
from mowdeck_control import MowerConnection, SimulatedMower, SimulationConfig
from mowdeck_service_kit.bus import InMemoryBus
from mowdeck_service_kit.testing import lifespan_client

from device_gateway.adapters.control.connections import RegistryConnectionProvider
from device_gateway.application.gateway import DeviceGateway
from device_gateway.application.session import SessionTiming
from device_gateway.domain.rules import Shard, same_link

NEW_BASE = "sim-base-2.0.0"


def _based(reg: MowerRegistration) -> MowerRegistration:
    ref = BaseStationRef(base_station_id=uuid4(), name="Garden base", source="simulated")
    return reg.model_copy(update={"base_station": ref})


class CountingBaseMower(SlowMower):
    """Counts the base station firmware checks the gateway asks for."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.base_checks = 0

    async def check_base_station_firmware(self) -> FirmwareInfo | None:
        self.base_checks += 1
        return await super().check_base_station_firmware()


def _factory(created: list[CountingBaseMower] | None = None) -> Any:
    def create(r: MowerRegistration) -> MowerConnection:
        mower = CountingBaseMower(
            mower_id=r.mower_id,
            account_id=r.account_id,
            config=SimulationConfig(time_scale=60, base_station_firmware_available=NEW_BASE),
            base_station=r.base_station,
        )
        if created is not None:
            created.append(mower)
        return mower

    return create


async def _ask(bus: InMemoryBus, reg: MowerRegistration, target: str) -> FirmwareCheckReply:
    request = FirmwareCheckRequest.model_validate(
        {"mower_id": reg.mower_id, "account_id": reg.account_id, "target": target}
    )
    reply = await bus.request(firmware_check_subject(reg.mower_id), request, timeout=3.0)
    return reply.model(FirmwareCheckReply)


def test_dg_base_station_reaches_the_connection_options() -> None:
    reg = _based(registration())
    assert reg.base_station is not None
    simulated = RegistryConnectionProvider().create(reg)
    assert isinstance(simulated, SimulatedMower)
    assert simulated.base_station == reg.base_station
    plain = RegistryConnectionProvider().create(registration())
    assert isinstance(plain, SimulatedMower) and plain.base_station is None
    assert BASE_STATION_OPTION not in registration().connection_options
    # Linking or unlinking a base station reopens the connection; an unchanged one does not.
    assert same_link(reg, reg) is True
    assert same_link(reg, reg.model_copy(update={"base_station": None})) is False


async def test_dg_base_station_state_and_firmware_check() -> None:
    bus = InMemoryBus()
    reg = _based(registration())
    plain = registration()
    app = make_app(
        bus=bus,
        provider=TestProvider({reg.mower_id: _factory()}),
        bootstrap_mowers=[reg.model_dump(mode="json"), plain.model_dump(mode="json")],
    )
    async with lifespan_client(app):
        await wait_until(lambda: telemetry(bus, reg.mower_id) and telemetry(bus, plain.mower_id))
        base = telemetry(bus, reg.mower_id)[-1].base_station
        assert base is not None and base.correction_link is RtkCorrectionLink.LORA
        assert reg.base_station is not None
        assert base.base_station_id == reg.base_station.base_station_id
        assert telemetry(bus, plain.mower_id)[-1].base_station is None

        reply = await _ask(bus, reg, "base_station")
        assert reply.info is not None and reply.info.update_state == "available"
        assert reply.info.available == NEW_BASE
        await wait_until(
            lambda: (
                (latest := telemetry(bus, reg.mower_id)[-1].base_station) is not None
                and latest.firmware_info is not None
                and latest.firmware_info.available == NEW_BASE
            )
        )
        mower_reply = await _ask(bus, reg, "mower")
        assert mower_reply.info is not None and mower_reply.info.installed == "sim-1.0.0"
        none = await _ask(bus, plain, "base_station")
        assert none.error is not None and none.error.code is FirmwareCheckErrorCode.UNSUPPORTED


async def test_dg_base_station_firmware_is_checked_periodically() -> None:
    bus = InMemoryBus()
    reg = _based(registration())
    created: list[CountingBaseMower] = []
    app = make_app(
        bus=bus,
        provider=TestProvider({reg.mower_id: _factory(created)}),
        bootstrap_mowers=[reg.model_dump(mode="json")],
        firmware_check_initial_seconds=0.05,
        firmware_check_interval_seconds=60,
    )
    async with lifespan_client(app):
        await wait_until(lambda: created and created[0].base_checks >= 1)
        await asyncio.sleep(0.2)
        assert created[0].base_checks == 1


async def test_dg_base_station_update_command_through_the_session() -> None:
    reg = _based(registration())
    provider = TestProvider({reg.mower_id: _factory()})
    published: list[Any] = []

    class Publisher:
        async def command_result(self, result: object) -> None:
            published.append(result)

        async def telemetry(self, state: object) -> None:
            return None

        async def connection_event(self, event_type: str, payload: object) -> None:
            return None

        async def map_snapshot(self, snapshot: object, fingerprint: str) -> None:
            return None

        async def capabilities_snapshot(self, snapshot: object, fingerprint: str) -> None:
            return None

        async def path_snapshot(self, snapshot: object, fingerprint: str) -> None:
            return None

    gateway = DeviceGateway(
        shard=Shard(),
        provider=provider,
        publisher=Publisher(),
        timing=SessionTiming(reconnect_initial_seconds=5, reconnect_max_seconds=5),
    )
    try:
        await gateway.register(reg, source="test")
        session = gateway.session(reg.mower_id)
        assert session is not None
        await wait_until(lambda: session.last_state is not None)
        await session.check_firmware("base_station")
        task = await gateway.submit(envelope(reg, BaseStationFirmwareUpdate(version=NEW_BASE)))
        assert task is not None
        result = await task
        assert result.status is CommandStatus.ACCEPTED, result.reason
        mower = provider.latest(reg.mower_id)
        assert isinstance(mower, SimulatedMower)
        base = mower.snapshot().base_station
        assert base is not None and base.firmware_info is not None
        assert base.firmware_info.update_state == "installing"
    finally:
        await gateway.close()
