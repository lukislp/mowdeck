"""Photo requests (``gateway.mower.<id>.photo``): image, size cap, tenancy, unsupported."""

import base64
from typing import Any
from uuid import uuid4

import pytest
from dg_support import TestProvider, make_app, registration, telemetry, wait_until
from mowdeck_contracts.v1 import (
    MowerRegistration,
    PhotoErrorCode,
    PhotoReply,
    PhotoRequest,
)
from mowdeck_contracts.v1.subjects import photo_subject
from mowdeck_control import MowerConnection, SimulatedMower, SimulationConfig
from mowdeck_service_kit.bus import InMemoryBus, RequestTimeoutError
from mowdeck_service_kit.testing import lifespan_client


class NoCamera:
    """A simulated mower behind a wrapper without ``capture_photo``."""

    def __init__(self, inner: SimulatedMower) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        if name == "capture_photo":
            raise AttributeError(name)
        return getattr(self._inner, name)


async def _ask(bus: InMemoryBus, reg: MowerRegistration, **values: Any) -> PhotoReply:
    request = PhotoRequest(mower_id=reg.mower_id, account_id=reg.account_id, **values)
    reply = await bus.request(photo_subject(reg.mower_id), request, timeout=2)
    return reply.model(PhotoReply)


async def test_dg_photo_request_returns_the_simulated_image() -> None:
    bus = InMemoryBus()
    reg = registration()
    app = make_app(bus=bus, bootstrap_mowers=[reg.model_dump(mode="json")])
    async with lifespan_client(app):
        await wait_until(lambda: telemetry(bus, reg.mower_id))
        reply = await _ask(bus, reg)
    assert reply.error is None
    assert reply.media_type == "image/jpeg"
    assert reply.image_base64 is not None
    assert base64.b64decode(reply.image_base64)[:2] == b"\xff\xd8"


async def test_dg_photo_request_of_a_foreign_account_is_not_found() -> None:
    bus = InMemoryBus()
    reg = registration()
    app = make_app(bus=bus, bootstrap_mowers=[reg.model_dump(mode="json")])
    async with lifespan_client(app):
        await wait_until(lambda: telemetry(bus, reg.mower_id))
        foreign = reg.model_copy(update={"account_id": uuid4()})
        reply = await _ask(bus, foreign)
    assert reply.error is not None
    assert reply.error.code is PhotoErrorCode.NOT_FOUND
    assert reply.image_base64 is None


async def test_dg_photo_request_is_capped_and_needs_a_capable_adapter() -> None:
    bus = InMemoryBus()
    reg = registration()
    plain = registration()

    def no_camera(registration: MowerRegistration) -> MowerConnection:
        sim = SimulatedMower(
            mower_id=registration.mower_id,
            account_id=registration.account_id,
            config=SimulationConfig(time_scale=60),
        )
        return NoCamera(sim)

    provider = TestProvider({plain.mower_id: no_camera})
    app = make_app(
        bus=bus,
        provider=provider,
        bootstrap_mowers=[reg.model_dump(mode="json"), plain.model_dump(mode="json")],
    )
    async with lifespan_client(app):
        await wait_until(lambda: telemetry(bus, reg.mower_id) and telemetry(bus, plain.mower_id))
        too_large = await _ask(bus, reg, max_bytes=1024)
        unsupported = await _ask(bus, plain)
    assert too_large.error is not None and too_large.error.code is PhotoErrorCode.TOO_LARGE
    assert unsupported.error is not None
    assert unsupported.error.code is PhotoErrorCode.UNSUPPORTED


async def test_dg_photo_request_for_a_mower_held_elsewhere_stays_unanswered() -> None:
    bus = InMemoryBus()
    app = make_app(bus=bus)
    async with lifespan_client(app):
        with pytest.raises(RequestTimeoutError):
            await _ask_unknown(bus)


async def _ask_unknown(bus: InMemoryBus) -> None:
    request = PhotoRequest(mower_id=uuid4(), account_id=uuid4())
    await bus.request(photo_subject(request.mower_id), request, timeout=0.2)
