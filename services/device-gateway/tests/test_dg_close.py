"""Every way a session ends closes its connection exactly once: removal, a link change, a
replacement, the gateway shutting down, and a removal while a close is still running."""

import asyncio
from typing import Any

from dg_support import SilentPublisher, SlowMower, TestProvider, registration, wait_until
from mowdeck_contracts.v1 import LINK_BLE_BRIDGE, MowerRegistration
from mowdeck_control import MowerConnection, SimulationConfig

from device_gateway.application.gateway import DeviceGateway
from device_gateway.application.session import SessionTiming
from device_gateway.domain.rules import ConnectionStatus, Shard


class CountingMower(SlowMower):
    """A simulated mower that counts its disconnects; ``close_delay`` makes them slow."""

    def __init__(self, *, close_delay: float = 0.0, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.close_delay = close_delay
        self.disconnects = 0
        self.disconnects_done = 0

    async def disconnect(self) -> None:
        self.disconnects += 1
        if self.close_delay:
            await asyncio.sleep(self.close_delay)
        await super().disconnect()
        self.disconnects_done += 1


def _counting(close_delay: float = 0.0) -> Any:
    def create(r: MowerRegistration) -> MowerConnection:
        return CountingMower(
            mower_id=r.mower_id,
            account_id=r.account_id,
            config=SimulationConfig(time_scale=60),
            close_delay=close_delay,
        )

    return create


def _gateway(provider: TestProvider, **timing: float) -> DeviceGateway:
    return DeviceGateway(
        shard=Shard(),
        provider=provider,
        publisher=SilentPublisher(),
        timing=SessionTiming(
            **{"reconnect_initial_seconds": 0.05, "connection_check_seconds": 0.01, **timing}
        ),
    )


def _connected(gateway: DeviceGateway, reg: MowerRegistration) -> bool:
    session = gateway.session(reg.mower_id)
    return session is not None and session.status is ConnectionStatus.CONNECTED


def _mower(provider: TestProvider, reg: MowerRegistration, index: int = -1) -> CountingMower:
    mower = provider.created[reg.mower_id][index]
    assert isinstance(mower, CountingMower)
    return mower


async def test_dg_removal_closes_the_connection_exactly_once() -> None:
    reg = registration()
    provider = TestProvider({reg.mower_id: _counting()})
    gateway = _gateway(provider)
    try:
        await gateway.register(reg, source="test")
        await wait_until(lambda: _connected(gateway, reg))
        assert await gateway.remove(reg.mower_id, source="test")
        mower = _mower(provider, reg)
        assert mower.disconnects == 1 and not mower.is_connected
        assert not await gateway.remove(reg.mower_id, source="test")
    finally:
        await gateway.close()
    assert _mower(provider, reg).disconnects == 1  # the shutdown does not close it again


async def test_dg_link_change_and_replacement_close_the_old_connection_once() -> None:
    reg = registration()
    provider = TestProvider({reg.mower_id: _counting()})
    gateway = _gateway(provider)
    try:
        await gateway.register(reg, source="test")
        await wait_until(lambda: _connected(gateway, reg))
        bridged = reg.model_copy(
            update={"links": {LINK_BLE_BRIDGE: {"host": "bridge.local", "port": 6053}}}
        )
        await gateway.register(bridged, source="test")  # a link added
        assert _mower(provider, reg, 0).disconnects == 1
        await wait_until(lambda: _connected(gateway, reg))
        await gateway.register(reg, source="test")  # the link removed
        assert _mower(provider, reg, 1).disconnects == 1
        await wait_until(lambda: _connected(gateway, reg))
        relinked = reg.model_copy(update={"connection_options": {"battery_percent": 40}})
        await gateway.register(relinked, source="test")  # replaced on new options
        assert _mower(provider, reg, 2).disconnects == 1
        await wait_until(lambda: _connected(gateway, reg))
    finally:
        await gateway.close()
    assert [_mower(provider, reg, i).disconnects for i in range(4)] == [1, 1, 1, 1]


async def test_dg_removal_during_a_running_close_lets_the_close_finish() -> None:
    """The link dropped and its close is still running when the mower is removed: the
    removal must not cut the close short (the bridge would stay open)."""
    reg = registration()
    provider = TestProvider({reg.mower_id: _counting(close_delay=0.2)})
    gateway = _gateway(provider, reconnect_initial_seconds=5, reconnect_max_seconds=5)
    try:
        await gateway.register(reg, source="test")
        await wait_until(lambda: _connected(gateway, reg))
        mower = _mower(provider, reg)
        mower._connected = False  # the link drops, the session closes it
        await wait_until(lambda: mower.disconnects == 1)
        assert await gateway.remove(reg.mower_id, source="test")
        assert mower.disconnects_done == 1
        assert len(provider.created[reg.mower_id]) == 1
    finally:
        await gateway.close()


class SpuriousCancel(SlowMower):
    """A connection whose library cancels one of its own futures on the first connect."""

    attempts = 0

    async def connect(self) -> None:
        SpuriousCancel.attempts += 1
        if SpuriousCancel.attempts == 1:
            raise asyncio.CancelledError
        await super().connect()


async def test_dg_cancelled_error_from_a_library_reconnects_instead_of_ending_the_session() -> None:
    reg = registration()

    def create(r: MowerRegistration) -> MowerConnection:
        return SpuriousCancel(
            mower_id=r.mower_id, account_id=r.account_id, config=SimulationConfig(time_scale=60)
        )

    SpuriousCancel.attempts = 0
    provider = TestProvider({reg.mower_id: create})
    gateway = _gateway(provider)
    try:
        await gateway.register(reg, source="test")
        await wait_until(lambda: _connected(gateway, reg))
        assert len(provider.created[reg.mower_id]) == 2
    finally:
        await gateway.close()
