"""Camera session requests (ADR 0032) through the in-memory bus."""

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
from mowdeck_contracts.v1 import (
    CameraProvider,
    CameraSessionErrorCode,
    CameraSessionReply,
    CameraSessionRequest,
    MowerRegistration,
)
from mowdeck_contracts.v1.subjects import camera_session_subject
from mowdeck_control import MowerConnection, SimulationConfig
from mowdeck_service_kit.bus import InMemoryBus, RequestTimeoutError
from mowdeck_service_kit.testing import lifespan_client

from device_gateway.application.gateway import DeviceGateway
from device_gateway.application.session import SessionTiming
from device_gateway.domain.rules import Shard


async def _ask(bus: InMemoryBus, request: CameraSessionRequest) -> CameraSessionReply:
    reply = await bus.request(camera_session_subject(request.mower_id), request, timeout=1.0)
    return reply.model(CameraSessionReply)


def _request(reg: MowerRegistration, **changes: object) -> CameraSessionRequest:
    values: dict[str, object] = {
        "mower_id": reg.mower_id,
        "account_id": reg.account_id,
        "requested_by": ISSUER,
    }
    values.update(changes)
    return CameraSessionRequest.model_validate(values)


async def test_dg_camera_session_of_a_simulated_mower_is_fake() -> None:
    bus = InMemoryBus()
    reg = registration()
    app = make_app(bus=bus, bootstrap_mowers=[reg.model_dump(mode="json")])
    async with lifespan_client(app):
        await wait_until(lambda: telemetry(bus, reg.mower_id))
        reply = await _ask(bus, _request(reg))
        assert reply.session is not None
        assert reply.session.provider is CameraProvider.FAKE
        assert reply.session.channel == f"sim-{reg.mower_id}"
        refused = await _ask(bus, _request(reg, account_id=uuid4()))
        assert refused.error is not None
        assert refused.error.code is CameraSessionErrorCode.FORBIDDEN
        unknown = await _ask(bus, _request(registration()))
        assert unknown.error is not None
        assert unknown.error.code is CameraSessionErrorCode.NOT_FOUND
        registry = app.state.metrics_registry
        metric = "mowdeck_device_gateway_camera_sessions_total"
        assert registry.get_sample_value(metric, {"outcome": "issued"}) == 1
        assert registry.get_sample_value(metric, {"outcome": "forbidden"}) == 1
        assert registry.get_sample_value(metric, {"outcome": "not_found"}) == 1


async def test_dg_camera_session_without_camera_is_unsupported() -> None:
    bus = InMemoryBus()
    reg = registration()

    def no_camera(r: MowerRegistration) -> MowerConnection:
        return SlowMower(
            mower_id=r.mower_id, account_id=r.account_id, config=SimulationConfig(camera=False)
        )

    provider = TestProvider({reg.mower_id: no_camera})
    app = make_app(bus=bus, provider=provider, bootstrap_mowers=[reg.model_dump(mode="json")])
    async with lifespan_client(app):
        await wait_until(lambda: telemetry(bus, reg.mower_id))
        reply = await _ask(bus, _request(reg))
        assert reply.error is not None
        assert reply.error.code is CameraSessionErrorCode.UNSUPPORTED


async def test_dg_camera_session_for_another_shard_gets_no_reply() -> None:
    bus = InMemoryBus()
    app = make_app(bus=bus, shard_index=0, shard_count=2)
    async with lifespan_client(app):
        other = registration(mower_id_in_shard(1, 2))
        with pytest.raises(RequestTimeoutError):
            await bus.request(camera_session_subject(other.mower_id), _request(other), timeout=0.2)


async def test_dg_camera_request_with_a_mismatched_subject_is_dropped() -> None:
    bus = InMemoryBus()
    reg = registration()
    app = make_app(bus=bus, bootstrap_mowers=[reg.model_dump(mode="json")])
    async with lifespan_client(app):
        with pytest.raises(RequestTimeoutError):
            await bus.request(camera_session_subject(uuid4()), _request(reg), timeout=0.2)


async def test_dg_camera_session_while_disconnected_is_unavailable() -> None:
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
        reply = await gateway.camera_session(_request(reg))
        assert reply is not None and reply.error is not None
        assert reply.error.code is CameraSessionErrorCode.UNAVAILABLE
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
