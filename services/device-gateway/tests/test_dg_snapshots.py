"""Map and capability snapshots on device.mower.<id>.* (stream DEVICE)."""

import asyncio
from typing import Any

from dg_support import (
    SlowMower,
    TestProvider,
    envelope,
    make_app,
    registration,
    result_for,
    telemetry,
    wait_until,
)
from mowdeck_contracts.v1 import (
    CommandStatus,
    MowerCapabilities,
    MowerCapabilitiesSnapshot,
    MowerMap,
    MowerMapSnapshot,
    SyncMap,
)
from mowdeck_contracts.v1.subjects import device_subject
from mowdeck_control import MaintenanceCommand, Outcome, SimulationConfig
from mowdeck_service_kit.bus import InMemoryBus, publish_command
from mowdeck_service_kit.testing import lifespan_client

from device_gateway.application.session import fingerprint


def _maps(bus: InMemoryBus, reg: Any) -> list[MowerMapSnapshot]:
    return [m.model(MowerMapSnapshot) for m in bus.messages(device_subject(reg.mower_id, "map"))]


def _caps(bus: InMemoryBus, reg: Any) -> list[MowerCapabilitiesSnapshot]:
    subject = device_subject(reg.mower_id, "capabilities")
    return [m.model(MowerCapabilitiesSnapshot) for m in bus.messages(subject)]


async def test_snapshots_are_published_once_after_connect() -> None:
    bus = InMemoryBus()
    reg = registration()
    app = make_app(bus=bus, bootstrap_mowers=[reg.model_dump(mode="json")])
    async with lifespan_client(app):
        await wait_until(lambda: _maps(bus, reg) and _caps(bus, reg))
        await wait_until(lambda: telemetry(bus, reg.mower_id))
        await asyncio.sleep(0.2)
        maps, caps = _maps(bus, reg), _caps(bus, reg)
    assert len(maps) == 1
    assert len(caps) == 1
    assert maps[0].account_id == reg.account_id
    assert [a.name for a in maps[0].map.areas] == ["Front lawn", "Back lawn"]
    assert caps[0].capabilities.blade_height_range_mm == (20, 100)
    assert caps[0].capabilities.max_speed_mps == 1.0


async def test_snapshot_msg_id_follows_the_content() -> None:
    bus = InMemoryBus()
    reg = registration()
    app = make_app(bus=bus, bootstrap_mowers=[reg.model_dump(mode="json")])
    async with lifespan_client(app):
        await wait_until(lambda: _maps(bus, reg))
        message = bus.messages(device_subject(reg.mower_id, "map"))[0]
        snap = message.model(MowerMapSnapshot)
    assert message.msg_id == f"{reg.mower_id}:map:{fingerprint(snap)}"


class DeviceSubjectsDown(InMemoryBus):
    """A bus that refuses every device snapshot."""

    async def publish(self, subject: str, payload: Any, **kwargs: Any) -> Any:
        if subject.startswith("device."):
            raise ConnectionError("stream DEVICE unavailable")
        return await super().publish(subject, payload, **kwargs)


async def test_a_failing_snapshot_publish_keeps_the_connection() -> None:
    bus = DeviceSubjectsDown()
    reg = registration()
    provider = TestProvider()
    app = make_app(bus=bus, provider=provider, bootstrap_mowers=[reg.model_dump(mode="json")])
    async with lifespan_client(app):
        await wait_until(lambda: telemetry(bus, reg.mower_id))
        await asyncio.sleep(0.1)
        assert _maps(bus, reg) == []
        assert len(provider.created[reg.mower_id]) == 1  # no reconnect


class LateModelMower(SlowMower):
    """A mower that learns its model and limits only after it is connected (like a real one)."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.learned = False

    async def capabilities(self) -> MowerCapabilities:
        caps = await super().capabilities()
        if not self.learned:
            return caps.model_copy(
                update={"model": "unknown", "blade_height_range_mm": None, "max_speed_mps": None}
            )
        return caps.model_copy(update={"model": "LUBA 2 AWD 3000X"})


async def test_capabilities_learned_after_connect_are_republished() -> None:
    bus = InMemoryBus()
    reg = registration()
    mowers: list[LateModelMower] = []

    def factory(r: Any) -> LateModelMower:
        mower = LateModelMower(
            mower_id=r.mower_id, account_id=r.account_id, config=SimulationConfig(time_scale=60)
        )
        mowers.append(mower)
        return mower

    provider = TestProvider({reg.mower_id: factory})
    app = make_app(
        bus=bus,
        provider=provider,
        bootstrap_mowers=[reg.model_dump(mode="json")],
        snapshot_recheck_seconds=0.1,
        telemetry_heartbeat_seconds=0.2,
    )
    async with lifespan_client(app):
        await wait_until(lambda: _caps(bus, reg))
        assert _caps(bus, reg)[-1].capabilities.blade_height_range_mm is None
        mowers[0].learned = True
        await wait_until(lambda: len(_caps(bus, reg)) >= 2)
        await asyncio.sleep(0.5)
        caps = _caps(bus, reg)
    assert len(caps) == 2, "the recheck publishes once per change, not per state"
    assert caps[-1].capabilities.model == "LUBA 2 AWD 3000X"
    assert caps[-1].capabilities.blade_height_range_mm == (20, 100)


class LateDockMower(SlowMower):
    """A mower whose dock arrives after the map was loaded (the Mammotion dock buffer), with a
    cheap rebuild (``MapRebuildable``) next to the full ``get_map``."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.dock_known = False
        self.get_map_calls = 0

    def _current(self, full: MowerMap) -> MowerMap:
        return full if self.dock_known else full.model_copy(update={"dock": None})

    async def get_map(self) -> MowerMap:
        self.get_map_calls += 1
        return self._current(await super().get_map())

    def current_map(self) -> MowerMap | None:
        return self._current(self._map)


async def test_a_dock_learned_after_connect_is_republished_without_a_map_reload() -> None:
    bus = InMemoryBus()
    reg = registration()
    mowers: list[LateDockMower] = []

    def factory(r: Any) -> LateDockMower:
        mower = LateDockMower(
            mower_id=r.mower_id, account_id=r.account_id, config=SimulationConfig(time_scale=60)
        )
        mowers.append(mower)
        return mower

    provider = TestProvider({reg.mower_id: factory})
    app = make_app(
        bus=bus,
        provider=provider,
        bootstrap_mowers=[reg.model_dump(mode="json")],
        snapshot_recheck_seconds=0.1,
        telemetry_heartbeat_seconds=0.2,
    )
    async with lifespan_client(app):
        await wait_until(lambda: _maps(bus, reg))
        assert _maps(bus, reg)[-1].map.dock is None
        mowers[0].dock_known = True  # the dock buffer arrived
        await wait_until(lambda: len(_maps(bus, reg)) >= 2)
        await asyncio.sleep(0.5)
        maps = _maps(bus, reg)
    assert len(maps) == 2, "the recheck publishes once per change, not per state"
    assert maps[-1].map.dock is not None
    assert mowers[0].get_map_calls == 1, "a recheck rebuilds; it never re-reads the map"


class RenamedInAppMower(SlowMower):
    """A mower whose zones were renamed in the vendor app: only a map sync shows it."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.synced = False

    async def run_maintenance(self, command: MaintenanceCommand) -> Outcome:
        if isinstance(command, SyncMap):
            self.synced = True
        return await super().run_maintenance(command)

    async def get_map(self) -> MowerMap:
        full = await super().get_map()
        if not self.synced:
            return full
        areas = [a.model_copy(update={"name": f"{a.name} (new)"}) for a in full.areas]
        return full.model_copy(update={"areas": areas})


async def test_sync_map_publishes_the_new_map_at_once() -> None:
    bus = InMemoryBus()
    reg = registration()

    def factory(r: Any) -> RenamedInAppMower:
        return RenamedInAppMower(
            mower_id=r.mower_id, account_id=r.account_id, config=SimulationConfig(time_scale=60)
        )

    app = make_app(
        bus=bus,
        provider=TestProvider({reg.mower_id: factory}),
        bootstrap_mowers=[reg.model_dump(mode="json")],
        snapshot_recheck_seconds=3600,  # only the command publishes
    )
    command = envelope(reg, SyncMap())
    async with lifespan_client(app):
        await wait_until(lambda: _maps(bus, reg))
        await publish_command(bus, command)
        await wait_until(lambda: len(_maps(bus, reg)) >= 2)
        result = result_for(bus, command)
        maps = _maps(bus, reg)
    assert result is not None and result.status is CommandStatus.EXECUTED
    assert [a.name for a in maps[-1].map.areas] == ["Front lawn (new)", "Back lawn (new)"]
