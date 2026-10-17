"""Camera release requests (ADR 0045) through the in-memory bus."""

from uuid import uuid4

import pytest
from dg_support import (
    ISSUER,
    FailingConnection,
    SlowMower,
    TestProvider,
    make_app,
    mower_id_in_shard,
    registration,
    telemetry,
    wait_until,
)
from fastapi import FastAPI
from mowdeck_contracts.v1 import (
    CameraReleaseReply,
    CameraReleaseRequest,
    CameraSessionErrorCode,
    MowerRegistration,
)
from mowdeck_contracts.v1.subjects import camera_release_subject
from mowdeck_control import CameraSessionRelease, MowerConnection, NotConnected
from mowdeck_service_kit.bus import InMemoryBus, RequestTimeoutError
from mowdeck_service_kit.testing import lifespan_client

from device_gateway.application.gateway import DeviceGateway
from device_gateway.application.session import SessionTiming
from device_gateway.domain.rules import Shard

METRIC = "mowdeck_device_gateway_camera_releases_total"


async def _ask(bus: InMemoryBus, request: CameraReleaseRequest) -> CameraReleaseReply:
    reply = await bus.request(camera_release_subject(request.mower_id), request, timeout=1.0)
    return reply.model(CameraReleaseReply)


def _request(reg: MowerRegistration, **changes: object) -> CameraReleaseRequest:
    values: dict[str, object] = {
        "mower_id": reg.mower_id,
        "account_id": reg.account_id,
        "requested_by": ISSUER,
        "channel": f"sim-{reg.mower_id}",
    }
    values.update(changes)
    return CameraReleaseRequest.model_validate(values)


class NoReleaseMower(SlowMower):
    """A camera mower whose adapter cannot stop the stream."""

    release_camera_session = None  # type: ignore[assignment]


class BrokenReleaseMower(SlowMower):
    """A mower whose stream stop fails like an unexpected vendor error."""

    async def release_camera_session(self, channel: str | None = None) -> None:
        raise RuntimeError("vendor exploded")


class UnsentReleaseMower(SlowMower):
    """A mower whose stream stop could not be sent."""

    async def release_camera_session(self, channel: str | None = None) -> None:
        raise NotConnected("the vendor cloud is down")


def _app_with(bus: InMemoryBus, reg: MowerRegistration, cls: type[SlowMower]) -> FastAPI:
    created: list[SlowMower] = []

    def factory(r: MowerRegistration) -> MowerConnection:
        mower = cls(mower_id=r.mower_id, account_id=r.account_id)
        created.append(mower)
        return mower

    provider = TestProvider({reg.mower_id: factory})
    app = make_app(bus=bus, provider=provider, bootstrap_mowers=[reg.model_dump(mode="json")])
    app.state.test_mowers = created
    return app


async def test_dg_camera_release_reaches_the_adapter_and_checks_the_account() -> None:
    bus = InMemoryBus()
    reg = registration()
    app = _app_with(bus, reg, SlowMower)
    async with lifespan_client(app):
        await wait_until(lambda: telemetry(bus, reg.mower_id))
        mower: SlowMower = app.state.test_mowers[-1]
        assert isinstance(mower, CameraSessionRelease)
        await mower.get_camera_session()
        assert mower.camera_publishing

        reply = await _ask(bus, _request(reg))
        assert reply.released is True and reply.error is None
        assert mower.camera_releases == [f"sim-{reg.mower_id}"]
        assert not mower.camera_publishing
        # Idempotent: a second release without a channel is answered the same way.
        again = await _ask(bus, _request(reg, channel=None))
        assert again.released is True
        assert mower.camera_releases == [f"sim-{reg.mower_id}", None]

        forbidden = await _ask(bus, _request(reg, account_id=uuid4()))
        assert forbidden.released is False
        assert forbidden.error is not None
        assert forbidden.error.code is CameraSessionErrorCode.FORBIDDEN
        unknown = await _ask(bus, _request(registration()))
        assert unknown.error is not None
        assert unknown.error.code is CameraSessionErrorCode.NOT_FOUND
        assert len(mower.camera_releases) == 2  # refused requests never reach the adapter

        registry = app.state.metrics_registry
        assert registry.get_sample_value(METRIC, {"outcome": "released"}) == 2
        assert registry.get_sample_value(METRIC, {"outcome": "forbidden"}) == 1
        assert registry.get_sample_value(METRIC, {"outcome": "not_found"}) == 1


async def test_dg_camera_release_without_the_port_is_unsupported_not_an_error() -> None:
    bus = InMemoryBus()
    reg = registration()
    app = _app_with(bus, reg, NoReleaseMower)
    async with lifespan_client(app):
        await wait_until(lambda: telemetry(bus, reg.mower_id))
        assert not isinstance(app.state.test_mowers[-1], CameraSessionRelease)
        reply = await _ask(bus, _request(reg))
        assert reply.released is False
        assert reply.error is None
        registry = app.state.metrics_registry
        assert registry.get_sample_value(METRIC, {"outcome": "unsupported"}) == 1


@pytest.mark.parametrize("cls", [BrokenReleaseMower, UnsentReleaseMower])
async def test_dg_camera_release_failure_is_unavailable_and_keeps_the_session(
    cls: type[SlowMower],
) -> None:
    bus = InMemoryBus()
    reg = registration()
    app = _app_with(bus, reg, cls)
    async with lifespan_client(app):
        await wait_until(lambda: telemetry(bus, reg.mower_id))
        reply = await _ask(bus, _request(reg))
        assert reply.released is False
        assert reply.error is not None
        assert reply.error.code is CameraSessionErrorCode.UNAVAILABLE
        assert "exploded" not in reply.error.detail
        # The mower's session survives: a camera session is still answered.
        gateway: DeviceGateway = app.state.gateway
        session = gateway._sessions[reg.mower_id]
        assert await session.camera_session() is not None
        assert len(app.state.test_mowers) == 1


async def test_dg_camera_release_for_another_shard_gets_no_reply() -> None:
    bus = InMemoryBus()
    app = make_app(bus=bus, shard_index=0, shard_count=2)
    async with lifespan_client(app):
        other = registration(mower_id_in_shard(1, 2))
        with pytest.raises(RequestTimeoutError):
            await bus.request(camera_release_subject(other.mower_id), _request(other), timeout=0.2)


async def test_dg_camera_release_with_a_mismatched_subject_is_dropped() -> None:
    bus = InMemoryBus()
    reg = registration()
    app = make_app(bus=bus, bootstrap_mowers=[reg.model_dump(mode="json")])
    async with lifespan_client(app):
        with pytest.raises(RequestTimeoutError):
            await bus.request(camera_release_subject(uuid4()), _request(reg), timeout=0.2)


async def test_dg_camera_release_while_disconnected_is_unavailable() -> None:
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
        reply = await gateway.camera_release(_request(reg))
        assert reply is not None and reply.error is not None
        assert reply.error.code is CameraSessionErrorCode.UNAVAILABLE
        assert reply.released is False
    finally:
        await gateway.close()


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
