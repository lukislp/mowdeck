"""Bridge scans, firmware checks and link secrets (ADR 0038) against the simulator."""

import asyncio
from typing import Any
from uuid import uuid4

import pytest
from dg_support import (
    FailingConnection,
    SlowMower,
    TestProvider,
    make_app,
    mower_id_in_shard,
    registration,
    telemetry,
    wait_until,
)
from mowdeck_contracts.v1 import (
    LINK_BLE_BRIDGE,
    BridgeScanErrorCode,
    BridgeScanReply,
    BridgeScanRequest,
    ConnectionKind,
    FirmwareCheckErrorCode,
    FirmwareCheckReply,
    FirmwareCheckRequest,
    FirmwareInfo,
    MowerCapabilities,
    MowerCapabilitiesSnapshot,
    MowerRegistration,
    ScannedDevice,
)
from mowdeck_contracts.v1.subjects import (
    bridge_scan_subject,
    device_subject,
    firmware_check_subject,
)
from mowdeck_control import MowerConnection, SimulatedMower, SimulationConfig
from mowdeck_service_kit.bus import InMemoryBus, RequestTimeoutError
from mowdeck_service_kit.testing import lifespan_client

from device_gateway.application.gateway import DeviceGateway
from device_gateway.application.ports import MowerCredentials
from device_gateway.application.session import MowerSession, SessionTiming
from device_gateway.domain.rules import (
    Shard,
    bluetooth_bridge_of,
    bridge_in_use,
    holds_bluetooth_through,
    links_changed,
    scan_found,
)

KEY = "ZTJlLW5vaXNlLWtleS1vZi0zMi1ieXRlcy0xMjM0NTY="
BRIDGE_LINK: dict[str, Any] = {
    "host": "192.0.2.10",
    "port": 6053,
    "ble_address": "FC:23:45:67:89:AB",
}
"""A bridge link as a bootstrap registration carries it (the key inline, no Fleet)."""
WITHHELD_LINK: dict[str, Any] = {**BRIDGE_LINK, "credentials": "fleet", "credentials_rev": "1"}
"""The same link as Fleet publishes it: the key withheld."""


def _bridged(reg: MowerRegistration, link: dict[str, Any] | None = None) -> MowerRegistration:
    """The registration with a bridge link."""
    return reg.model_copy(update={"links": {LINK_BLE_BRIDGE: dict(link or BRIDGE_LINK)}})


def _scan(reg: MowerRegistration, **changes: Any) -> BridgeScanRequest:
    values: dict[str, Any] = {
        "mower_id": reg.mower_id,
        "account_id": reg.account_id,
        "host": "192.0.2.10",
        "noise_psk": KEY,
        "expected_name": "Luba-SIM0001",
        "timeout_s": 2,
    }
    values.update(changes)
    return BridgeScanRequest.model_validate(values)


async def _ask_scan(bus: InMemoryBus, request: BridgeScanRequest) -> BridgeScanReply:
    reply = await bus.request(bridge_scan_subject(request.mower_id), request, timeout=3.0)
    return reply.model(BridgeScanReply)


async def _ask_firmware(
    bus: InMemoryBus, reg: MowerRegistration, **changes: Any
) -> FirmwareCheckReply:
    request = FirmwareCheckRequest(
        mower_id=reg.mower_id, account_id=changes.get("account_id", reg.account_id)
    )
    reply = await bus.request(firmware_check_subject(reg.mower_id), request, timeout=3.0)
    return reply.model(FirmwareCheckReply)


def _caps(bus: InMemoryBus, reg: MowerRegistration) -> list[MowerCapabilitiesSnapshot]:
    subject = device_subject(reg.mower_id, "capabilities")
    return [m.model(MowerCapabilitiesSnapshot) for m in bus.messages(subject)]


class NoLinksMower(SlowMower):
    """A connection without the optional capabilities (like a plain BLE adapter)."""

    async def capabilities(self) -> MowerCapabilities:
        caps = await super().capabilities()
        return caps.model_copy(update={"links_supported": [], "firmware_update": False})


class CountingMower(SlowMower):
    """Counts the firmware checks the gateway asks for."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.checks = 0

    async def check_firmware(self) -> FirmwareInfo | None:
        self.checks += 1
        return await super().check_firmware()


class FakeCredentials:
    """A credential source that answers with connection and link secrets."""

    def __init__(self, credentials: MowerCredentials) -> None:
        self.credentials = credentials
        self.calls = 0

    async def fetch(self, registration: MowerRegistration) -> MowerCredentials:
        self.calls += 1
        return self.credentials


class _NullPublisher:
    async def command_result(self, result: object) -> None:
        return None

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


# --- bridge scans ---------------------------------------------------------------------------


async def test_dg_bridge_scan_answers_through_the_connection() -> None:
    bus = InMemoryBus()
    reg = registration()
    other_account = registration()
    app = make_app(
        bus=bus,
        bootstrap_mowers=[reg.model_dump(mode="json"), other_account.model_dump(mode="json")],
    )
    async with lifespan_client(app):
        await wait_until(lambda: telemetry(bus, reg.mower_id))
        reply = await _ask_scan(bus, _scan(reg))
        assert reply.ok and reply.proxy is not None
        assert reply.proxy.proxy_name == "sim-bridge"
        assert [d.name for d in reply.devices] == ["Luba-SIM0001"]
        missing = await _ask_scan(bus, _scan(reg, expected_name="Luba-OTHER"))
        assert missing.error is not None and missing.error.code is BridgeScanErrorCode.TIMEOUT
        forbidden = await _ask_scan(bus, _scan(reg, account_id=uuid4()))
        assert forbidden.error is not None
        assert forbidden.error.code is BridgeScanErrorCode.FORBIDDEN
        unknown = await _ask_scan(bus, _scan(registration()))
        assert unknown.error is not None and unknown.error.code is BridgeScanErrorCode.NOT_FOUND
        registry = app.state.metrics_registry
        metric = "mowdeck_device_gateway_bridge_scans_total"
        assert registry.get_sample_value(metric, {"outcome": "ok"}) == 1
        assert registry.get_sample_value(metric, {"outcome": "timeout"}) == 1
        assert registry.get_sample_value(metric, {"outcome": "forbidden"}) == 1


async def test_dg_bridge_scan_is_unsupported_without_the_capability() -> None:
    bus = InMemoryBus()
    reg = registration()

    def plain(r: MowerRegistration) -> MowerConnection:
        return NoLinksMower(mower_id=r.mower_id, account_id=r.account_id)

    app = make_app(
        bus=bus,
        provider=TestProvider({reg.mower_id: plain}),
        bootstrap_mowers=[reg.model_dump(mode="json")],
    )
    async with lifespan_client(app):
        await wait_until(lambda: telemetry(bus, reg.mower_id))
        reply = await _ask_scan(bus, _scan(reg))
        assert reply.error is not None and reply.error.code is BridgeScanErrorCode.UNSUPPORTED
        firmware = await _ask_firmware(bus, reg)
        assert firmware.error is not None
        assert firmware.error.code is FirmwareCheckErrorCode.UNSUPPORTED


async def test_dg_bridge_scan_is_busy_while_another_mower_holds_the_bridge() -> None:
    bus = InMemoryBus()
    holder = _bridged(registration())
    asker = registration(account_id=holder.account_id)

    def bridged(r: MowerRegistration) -> MowerConnection:
        return SlowMower(
            mower_id=r.mower_id,
            account_id=r.account_id,
            config=SimulationConfig(time_scale=60, bridge=True),
        )

    app = make_app(
        bus=bus,
        provider=TestProvider({holder.mower_id: bridged}),
        bootstrap_mowers=[holder.model_dump(mode="json"), asker.model_dump(mode="json")],
    )
    async with lifespan_client(app):
        await wait_until(lambda: telemetry(bus, holder.mower_id) and telemetry(bus, asker.mower_id))
        held = telemetry(bus, holder.mower_id)[-1]
        assert held.connection is ConnectionKind.BLE_BRIDGE  # the simulated bridge is up
        assert held.links[LINK_BLE_BRIDGE].connected is True
        busy = await _ask_scan(bus, _scan(asker, host="192.0.2.10", expected_name=None))
        assert busy.error is not None and busy.error.code is BridgeScanErrorCode.BUSY
        # Looking for a mower while this gateway holds a Bluetooth connection through the
        # bridge: the mower does not advertise, the answer names the entry that holds it.
        held_here = await _ask_scan(bus, _scan(asker, host="192.0.2.10"))
        assert held_here.error is not None
        assert held_here.error.code is BridgeScanErrorCode.ALREADY_CONNECTED
        assert held_here.error.held_by == holder.name
        assert str(holder.mower_id) in held_here.error.detail
        # The holder itself re-checks its own bridge.
        own = await _ask_scan(bus, _scan(holder, host="192.0.2.10"))
        assert own.ok
        elsewhere = await _ask_scan(bus, _scan(asker, host="192.0.2.99"))
        assert elsewhere.ok


async def test_dg_bridge_scan_for_another_shard_gets_no_reply() -> None:
    bus = InMemoryBus()
    app = make_app(bus=bus, shard_index=0, shard_count=2)
    async with lifespan_client(app):
        other = registration(mower_id_in_shard(1, 2))
        with pytest.raises(RequestTimeoutError):
            await bus.request(bridge_scan_subject(other.mower_id), _scan(other), timeout=0.2)
        with pytest.raises(RequestTimeoutError):
            await bus.request(bridge_scan_subject(uuid4()), _scan(other), timeout=0.2)


async def test_dg_bridge_scan_while_disconnected_is_unsupported_not_an_error() -> None:
    reg = registration()

    def failing(r: MowerRegistration) -> MowerConnection:
        return FailingConnection(mower_id=r.mower_id, account_id=r.account_id)

    gateway = DeviceGateway(
        shard=Shard(),
        provider=TestProvider({reg.mower_id: failing}),
        publisher=_NullPublisher(),
        timing=SessionTiming(reconnect_initial_seconds=5, reconnect_max_seconds=5),
    )
    try:
        await gateway.register(reg, source="test")
        reply = await gateway.bridge_scan(_scan(reg))
        assert reply is not None and reply.error is not None
        assert reply.error.code is BridgeScanErrorCode.UNSUPPORTED
        firmware = await gateway.firmware_check(
            FirmwareCheckRequest(mower_id=reg.mower_id, account_id=reg.account_id)
        )
        assert firmware is not None and firmware.error is not None
        assert firmware.error.code is FirmwareCheckErrorCode.UNAVAILABLE
    finally:
        await gateway.close()


# --- firmware -------------------------------------------------------------------------------


async def test_dg_firmware_check_answers_and_publishes_the_state() -> None:
    bus = InMemoryBus()
    reg = registration()

    def offered(r: MowerRegistration) -> MowerConnection:
        return SlowMower(
            mower_id=r.mower_id,
            account_id=r.account_id,
            config=SimulationConfig(time_scale=60, firmware_available="sim-2.0.0"),
        )

    app = make_app(
        bus=bus,
        provider=TestProvider({reg.mower_id: offered}),
        bootstrap_mowers=[reg.model_dump(mode="json")],
    )
    async with lifespan_client(app):
        await wait_until(lambda: telemetry(bus, reg.mower_id))
        reply = await _ask_firmware(bus, reg)
        assert reply.info is not None and reply.error is None
        assert reply.info.update_state == "available"
        assert reply.info.available == "sim-2.0.0" and reply.info.installed == "sim-1.0.0"
        latest = telemetry(bus, reg.mower_id)[-1]
        assert latest.firmware_info is not None
        assert latest.firmware_info.available == "sim-2.0.0"
        forbidden = await _ask_firmware(bus, reg, account_id=uuid4())
        assert forbidden.error is not None
        assert forbidden.error.code is FirmwareCheckErrorCode.FORBIDDEN
        unknown = await _ask_firmware(bus, registration())
        assert unknown.error is not None and unknown.error.code is FirmwareCheckErrorCode.NOT_FOUND
        registry = app.state.metrics_registry
        metric = "mowdeck_device_gateway_firmware_checks_total"
        assert registry.get_sample_value(metric, {"outcome": "info"}) == 1
        assert registry.get_sample_value(metric, {"outcome": "forbidden"}) == 1


async def test_dg_firmware_is_checked_periodically_after_the_connect() -> None:
    bus = InMemoryBus()
    reg = registration()
    created: list[CountingMower] = []

    def counting(r: MowerRegistration) -> MowerConnection:
        mower = CountingMower(
            mower_id=r.mower_id, account_id=r.account_id, config=SimulationConfig(time_scale=60)
        )
        created.append(mower)
        return mower

    app = make_app(
        bus=bus,
        provider=TestProvider({reg.mower_id: counting}),
        bootstrap_mowers=[reg.model_dump(mode="json")],
        firmware_check_initial_seconds=0.05,
        firmware_check_interval_seconds=60,
    )
    async with lifespan_client(app):
        await wait_until(lambda: created and created[0].checks >= 1)
        await asyncio.sleep(0.2)
        assert created[0].checks == 1  # the next one is due in a minute


async def test_dg_firmware_checks_can_be_switched_off() -> None:
    bus = InMemoryBus()
    reg = registration()
    created: list[CountingMower] = []

    def counting(r: MowerRegistration) -> MowerConnection:
        mower = CountingMower(mower_id=r.mower_id, account_id=r.account_id)
        created.append(mower)
        return mower

    app = make_app(
        bus=bus,
        provider=TestProvider({reg.mower_id: counting}),
        bootstrap_mowers=[reg.model_dump(mode="json")],
        firmware_check_initial_seconds=0,
    )
    async with lifespan_client(app):
        await wait_until(lambda: telemetry(bus, reg.mower_id))
        await asyncio.sleep(0.2)
        assert created[0].checks == 0


# --- links --------------------------------------------------------------------------------


async def test_dg_link_secrets_are_merged_into_the_link_for_the_connect_only() -> None:
    reg = _bridged(registration(), WITHHELD_LINK).model_copy(
        update={"connection_options": {"credentials": "fleet", "credentials_rev": "3"}}
    )
    assert reg.needs_credentials and reg.links_needing_credentials == {LINK_BLE_BRIDGE}
    credentials = FakeCredentials(
        MowerCredentials(
            options={"time_scale": "60"}, link_secrets={LINK_BLE_BRIDGE: {"noise_psk": KEY}}
        )
    )
    provider = TestProvider()
    gateway = DeviceGateway(
        shard=Shard(),
        provider=provider,
        publisher=_NullPublisher(),
        timing=SessionTiming(reconnect_initial_seconds=5, reconnect_max_seconds=5),
        credentials=credentials,
    )
    try:
        await gateway.register(reg, source="test")
        await wait_until(lambda: provider.created.get(reg.mower_id))
        session = gateway.session(reg.mower_id)
        assert session is not None and session.registration == reg  # nothing kept
        connected = await session._connected_registration()
        assert connected.connection_options == {"time_scale": "60"}
        link = connected.links[LINK_BLE_BRIDGE]
        assert link["noise_psk"] == KEY and link["host"] == "192.0.2.10"
        assert "credentials" not in link and "credentials_rev" not in link
        assert credentials.calls >= 1
    finally:
        await gateway.close()


async def test_dg_registration_links_reach_the_connection_options() -> None:
    reg = _bridged(registration())
    provider = TestProvider()
    gateway = DeviceGateway(
        shard=Shard(),
        provider=provider,
        publisher=_NullPublisher(),
        timing=SessionTiming(reconnect_initial_seconds=5, reconnect_max_seconds=5),
    )
    try:
        await gateway.register(reg, source="test")
        await wait_until(lambda: provider.created.get(reg.mower_id))
        mower = provider.latest(reg.mower_id)
        assert isinstance(mower, SlowMower)
        # The registry provider (the real one) turns the links into the simulator's bridge.
        from device_gateway.adapters.control.connections import RegistryConnectionProvider

        simulated = RegistryConnectionProvider().create(reg)
        assert isinstance(simulated, SimulatedMower) and simulated.config.bridge is True
        plain = RegistryConnectionProvider().create(registration())
        assert isinstance(plain, SimulatedMower) and plain.config.bridge is False
        # A changed link reconnects the mower, a same one updates in place.
        assert await gateway.register(reg, source="test") is True
        assert len(provider.created[reg.mower_id]) == 1
        moved = reg.model_copy(
            update={"links": {LINK_BLE_BRIDGE: {**BRIDGE_LINK, "host": "192.0.2.11"}}}
        )
        assert await gateway.register(moved, source="test") is True
        await wait_until(lambda: len(provider.created[reg.mower_id]) == 2)
    finally:
        await gateway.close()


async def test_dg_link_change_republishes_the_capabilities() -> None:
    bus = InMemoryBus()
    reg = _bridged(registration())
    provider = TestProvider()
    app = make_app(bus=bus, provider=provider, bootstrap_mowers=[reg.model_dump(mode="json")])
    async with lifespan_client(app):
        await wait_until(lambda: telemetry(bus, reg.mower_id) and _caps(bus, reg))
        mower = provider.latest(reg.mower_id)
        assert isinstance(mower, SlowMower)
        # The test provider builds plain simulators: switch the bridge on through the config.
        assert _caps(bus, reg)[0].capabilities.manual_drive is True
        first = telemetry(bus, reg.mower_id)[-1]
        assert first.links == {}  # the TestProvider's mower has no bridge configured
    bus = InMemoryBus()

    def bridged(r: MowerRegistration) -> MowerConnection:
        return SlowMower(
            mower_id=r.mower_id,
            account_id=r.account_id,
            config=SimulationConfig(time_scale=60, bridge=True),
        )

    provider = TestProvider({reg.mower_id: bridged})
    app = make_app(bus=bus, provider=provider, bootstrap_mowers=[reg.model_dump(mode="json")])
    async with lifespan_client(app):
        await wait_until(lambda: telemetry(bus, reg.mower_id) and _caps(bus, reg))
        mower = provider.latest(reg.mower_id)
        assert isinstance(mower, SlowMower)
        assert telemetry(bus, reg.mower_id)[-1].connection is ConnectionKind.BLE_BRIDGE
        mower.set_bridge(False)
        await wait_until(lambda: len(_caps(bus, reg)) == 2)
        assert _caps(bus, reg)[1].capabilities.manual_drive is False
        await wait_until(
            lambda: telemetry(bus, reg.mower_id)[-1].connection is ConnectionKind.SIMULATED
        )
        mower.set_bridge(True)
        await wait_until(lambda: len(_caps(bus, reg)) == 3)
        assert _caps(bus, reg)[2].capabilities.manual_drive is True


def test_dg_rules_links_changed_and_bridge_in_use() -> None:
    reg = _bridged(registration())
    mower = SlowMower(
        mower_id=reg.mower_id, account_id=reg.account_id, config=SimulationConfig(bridge=True)
    )
    up = mower.snapshot()
    assert links_changed(None, up) is True
    assert links_changed(up, up) is False
    mower.set_bridge(False)
    down = mower.snapshot()
    assert links_changed(up, down) is True
    assert bridge_in_use(reg, up, "192.0.2.10", 6053) is True
    assert bridge_in_use(reg, up, "192.0.2.10", 6054) is False
    assert bridge_in_use(reg, down, "192.0.2.10", 6053) is False
    assert bridge_in_use(reg, None, "192.0.2.10", 6053) is False
    assert bridge_in_use(registration(), up, "192.0.2.10", 6053) is False
    assert holds_bluetooth_through(reg, up, "192.0.2.10", 6053) is True
    assert holds_bluetooth_through(reg, down, "192.0.2.10", 6053) is False
    assert holds_bluetooth_through(reg, up, "192.0.2.11", 6053) is False
    standalone = registration().model_copy(
        update={
            "connection_kind": ConnectionKind.BLE_BRIDGE,
            "connection_options": {"host": "Bridge.local", "ble_address": "AA:BB:CC:DD:EE:FF"},
        }
    )
    assert bluetooth_bridge_of(standalone) == ("bridge.local", 6053)
    assert holds_bluetooth_through(standalone, up, "bridge.local", 6053) is True
    assert bluetooth_bridge_of(registration()) is None
    seen = BridgeScanReply(
        devices=[ScannedDevice(name="Luba-A", address="AA:BB:CC:DD:EE:01", rssi=-60)]
    )
    assert scan_found(seen, "luba-a") and not scan_found(seen, "Luba-B")
    assert scan_found(seen, None) and not scan_found(BridgeScanReply(), None)


async def test_dg_bridge_scan_names_the_entry_that_holds_the_mower_after_an_empty_scan() -> None:
    """The asker's own scan ran and saw nothing; another entry holds the mower through the
    same bridge over Bluetooth (a standalone bridge connection, which reports no link)."""
    holder = registration(name="Garage mower")
    holder = holder.model_copy(
        update={
            "connection_kind": ConnectionKind.BLE_BRIDGE,
            "connection_options": {"host": "192.0.2.10", "ble_address": "AA:BB:CC:DD:EE:FF"},
        }
    )
    asker = registration(account_id=holder.account_id)

    def bluetooth(r: MowerRegistration) -> MowerConnection:
        return SlowMower(
            mower_id=r.mower_id,
            account_id=r.account_id,
            config=SimulationConfig(time_scale=60, bridge=True),
        )

    def empty_bridge(r: MowerRegistration) -> MowerConnection:
        return SlowMower(
            mower_id=r.mower_id,
            account_id=r.account_id,
            config=SimulationConfig(time_scale=60, bridge_devices=()),
        )

    gateway = DeviceGateway(
        shard=Shard(),
        provider=TestProvider({holder.mower_id: bluetooth, asker.mower_id: empty_bridge}),
        publisher=_NullPublisher(),
        timing=SessionTiming(reconnect_initial_seconds=5, reconnect_max_seconds=5),
    )
    try:
        await gateway.register(holder, source="test")
        await gateway.register(asker, source="test")

        def ready() -> bool:
            held, asking = gateway.session(holder.mower_id), gateway.session(asker.mower_id)
            return bool(held and held.last_state and asking and asking.last_state)

        await wait_until(ready)
        reply = await gateway.bridge_scan(_scan(asker))
        assert reply is not None and reply.error is not None
        assert reply.error.code is BridgeScanErrorCode.ALREADY_CONNECTED
        assert reply.error.held_by == "Garage mower"
        assert reply.proxy is not None  # the scan itself ran
        other = await gateway.bridge_scan(_scan(asker, host="192.0.2.99"))
        assert other is not None and other.error is not None
        assert other.error.code is BridgeScanErrorCode.TIMEOUT  # nobody holds that bridge
    finally:
        await gateway.close()


async def test_dg_session_check_firmware_needs_a_connection() -> None:
    reg = registration()
    session = MowerSession(
        reg, provider=TestProvider(), publisher=_NullPublisher(), timing=SessionTiming()
    )
    with pytest.raises(Exception, match="not connected"):
        await session.check_firmware()
    with pytest.raises(Exception, match="not connected"):
        await session.scan_bridge(host="h", port=1, secret=KEY, expected_name=None, timeout_s=1)
