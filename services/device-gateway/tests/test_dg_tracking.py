"""Tracking leases (ADR 0058): ``gateway.mower.<id>.tracking`` against the simulator."""

import asyncio
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
    MowerRegistration,
    TrackingLeaseCode,
    TrackingLeaseReply,
    TrackingLeaseRequest,
)
from mowdeck_contracts.v1.subjects import (
    parse_tracking_lease_subject,
    tracking_lease_subject,
)
from mowdeck_control import MowerConnection, SimulationConfig, TrackingRateCapable
from mowdeck_service_kit.bus import InMemoryBus, RequestTimeoutError
from mowdeck_service_kit.testing import lifespan_client

from device_gateway.application.gateway import DeviceGateway
from device_gateway.application.session import SessionTiming
from device_gateway.domain.rules import Shard


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


class RatedMower(SlowMower):
    """A simulated mower that, like the Mammotion cloud connection, can be asked for reports."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.intervals: list[float | None] = []

    def set_tracking_interval(self, interval_s: float | None) -> float | None:
        applied = None if interval_s is None else max(5.0, interval_s)
        self.intervals.append(applied)
        return applied


async def _lease(
    bus: InMemoryBus,
    reg: MowerRegistration,
    *,
    seconds: float = 60,
    interval: float = 10,
    **kw: object,
) -> TrackingLeaseReply:
    request = TrackingLeaseRequest(
        mower_id=reg.mower_id,
        account_id=kw.get("account_id", reg.account_id),  # type: ignore[arg-type]
        interval_seconds=interval,
        lease_seconds=seconds,
    )
    reply = await bus.request(tracking_lease_subject(reg.mower_id), request, timeout=2.0)
    return reply.model(TrackingLeaseReply)


def test_tracking_lease_subject_roundtrip() -> None:
    mower_id = uuid4()
    assert parse_tracking_lease_subject(tracking_lease_subject(mower_id)) == mower_id
    with pytest.raises(ValueError):
        parse_tracking_lease_subject("gateway.mower.x.photo")


def test_tracking_rate_capable_is_a_runtime_protocol() -> None:
    assert isinstance(RatedMower(mower_id=uuid4(), account_id=uuid4()), TrackingRateCapable)


async def test_dg_tracking_lease_takes_renews_releases_and_counts() -> None:
    bus = InMemoryBus()
    reg = registration()
    created: list[RatedMower] = []

    def rated(r: MowerRegistration) -> MowerConnection:
        mower = RatedMower(
            mower_id=r.mower_id, account_id=r.account_id, config=SimulationConfig(time_scale=60)
        )
        created.append(mower)
        return mower

    app = make_app(
        bus=bus,
        provider=TestProvider({reg.mower_id: rated}),
        bootstrap_mowers=[reg.model_dump(mode="json")],
    )
    async with lifespan_client(app):
        await wait_until(lambda: telemetry(bus, reg.mower_id))
        taken = await _lease(bus, reg, interval=2)
        assert taken.code is TrackingLeaseCode.OK
        assert taken.interval_seconds == 5.0  # the adapter clamps to what its link allows
        assert created[0].intervals == [5.0]
        await _lease(bus, reg)  # a renewal
        assert created[0].intervals[-1] == 10.0
        released = await _lease(bus, reg, seconds=0)
        assert released.code is TrackingLeaseCode.OK
        assert created[0].intervals[-1] is None  # the normal rhythm again
        foreign = await _lease(bus, reg, account_id=uuid4())
        assert foreign.code is TrackingLeaseCode.NOT_FOUND
        unknown = await _lease(bus, registration())
        assert unknown.code is TrackingLeaseCode.NOT_FOUND
        metric = "mowdeck_device_gateway_tracking_leases_total"
        assert app.state.metrics_registry.get_sample_value(metric, {"outcome": "ok"}) == 3
        assert app.state.metrics_registry.get_sample_value(metric, {"outcome": "not_found"}) == 2


async def test_dg_tracking_lease_expires_without_renewal() -> None:
    bus = InMemoryBus()
    reg = registration()
    created: list[RatedMower] = []

    def rated(r: MowerRegistration) -> MowerConnection:
        mower = RatedMower(mower_id=r.mower_id, account_id=r.account_id)
        created.append(mower)
        return mower

    app = make_app(
        bus=bus,
        provider=TestProvider({reg.mower_id: rated}),
        bootstrap_mowers=[reg.model_dump(mode="json")],
    )
    async with lifespan_client(app):
        await wait_until(lambda: telemetry(bus, reg.mower_id))
        await _lease(bus, reg, seconds=0.2)
        assert created[0].intervals == [10.0]
        await wait_until(lambda: created[0].intervals[-1] is None, within=3)  # fell back


async def test_dg_tracking_lease_on_a_connection_that_reports_on_its_own() -> None:
    bus = InMemoryBus()
    reg = registration()
    app = make_app(bus=bus, bootstrap_mowers=[reg.model_dump(mode="json")])
    async with lifespan_client(app):
        await wait_until(lambda: telemetry(bus, reg.mower_id))
        reply = await _lease(bus, reg, interval=3)
        assert reply.code is TrackingLeaseCode.OK
        assert reply.detail == "the mower reports on its own"
        assert reply.interval_seconds == 3  # the state heartbeat follows the interval
        await _lease(bus, reg, seconds=0)


async def test_dg_tracking_lease_while_not_connected_and_for_another_shard() -> None:
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
        reply = await gateway.tracking_lease(
            TrackingLeaseRequest(mower_id=reg.mower_id, account_id=reg.account_id)
        )
        assert reply is not None and reply.code is TrackingLeaseCode.NOT_CONNECTED
    finally:
        await gateway.close()

    bus = InMemoryBus()
    app = make_app(bus=bus, shard_index=0, shard_count=2)
    async with lifespan_client(app):
        other = registration(mower_id_in_shard(1, 2))
        with pytest.raises(RequestTimeoutError):
            await bus.request(
                tracking_lease_subject(other.mower_id),
                TrackingLeaseRequest(mower_id=other.mower_id, account_id=other.account_id),
                timeout=0.2,
            )
        await asyncio.sleep(0)
