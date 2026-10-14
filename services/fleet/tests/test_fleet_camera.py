"""``POST /mowers/{id}/camera/session`` (ADR 0032): checks, relay to the gateway, audit."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fleet_support import API, harness
from mowdeck_authz import mower
from mowdeck_contracts.v1 import (
    CameraProvider,
    CameraSession,
    CameraSessionError,
    CameraSessionErrorCode,
    CameraSessionReply,
    CameraSessionRequest,
)
from mowdeck_contracts.v1.subjects import ALL_CAMERA_SESSIONS
from mowdeck_service_kit.bus import BusMessage, InMemoryBus
from mowdeck_service_kit.testing import FakeEntitlements

from fleet.application.camera import CAMERA_OPENED

SECRET = "fake-token-123"  # noqa: S105 - a test value


def _session() -> CameraSession:
    return CameraSession(
        provider=CameraProvider.FAKE,
        app_id="",
        channel="sim",
        token=SECRET,
        uid=0,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )


class FakeCameraGateway:
    """Answers camera requests like the Device-Gateway; ``reply`` None stays silent."""

    def __init__(self) -> None:
        self.reply: CameraSessionReply | None = CameraSessionReply(session=_session())
        self.requests: list[CameraSessionRequest] = []

    async def handle(self, message: BusMessage) -> None:
        self.requests.append(message.model(CameraSessionRequest))
        if self.reply is not None:
            await message.respond(self.reply)


async def _gateway(bus: InMemoryBus) -> FakeCameraGateway:
    fake = FakeCameraGateway()
    await bus.subscribe(ALL_CAMERA_SESSIONS, fake.handle)
    return fake


def _url(mower_id: str | UUID) -> str:
    return f"{API}/mowers/{mower_id}/camera/session"


async def test_fleet_camera_session_is_relayed_audited_and_not_cached() -> None:
    bus = InMemoryBus()
    async with harness(bus=bus) as h:
        fake = await _gateway(bus)
        owner = await h.owner()
        viewer = await h.member(owner, "viewer")
        created = await h.register(owner)
        response = await h.client.post(_url(created["id"]), headers=viewer.headers)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        body: dict[str, Any] = response.json()
        assert body["provider"] == "fake" and body["token"] == SECRET
        (request,) = fake.requests
        assert str(request.mower_id) == created["id"]
        assert request.account_id == owner.account_id
        assert request.requested_by.subject == viewer.principal.subject
        async with h.store.transaction() as tx:
            (row,) = await tx.audit.find(action=CAMERA_OPENED)
        assert row.target_id == created["id"]
        assert row.actor_subject == f"user:{viewer.principal.subject}"
        assert row.details["provider"] == "fake"
        assert SECRET not in str(row.details)


async def test_fleet_camera_session_hides_foreign_mowers() -> None:
    bus = InMemoryBus()
    async with harness(bus=bus) as h:
        fake = await _gateway(bus)
        owner = await h.owner()
        stranger = await h.owner()
        created = await h.register(owner)
        response = await h.client.post(_url(created["id"]), headers=stranger.headers)
        assert response.status_code == 404
        assert fake.requests == []


async def test_fleet_camera_session_needs_a_view_grant_on_the_mower() -> None:
    bus = InMemoryBus()
    async with harness(bus=bus) as h:
        await _gateway(bus)
        owner = await h.owner()
        first = await h.register(owner)
        second = await h.register(owner)
        limited = await h.member(owner, "viewer", mower(UUID(first["id"])))
        assert (await h.client.post(_url(first["id"]), headers=limited.headers)).status_code == 200
        hidden = await h.client.post(_url(second["id"]), headers=limited.headers)
        assert hidden.status_code == 404


async def test_fleet_camera_session_needs_the_plan_feature() -> None:
    bus = InMemoryBus()
    plans = FakeEntitlements(plan="crafted", features=["view", "control", "emergency_stop"])
    async with harness(bus=bus, entitlements=plans) as h:
        fake = await _gateway(bus)
        owner = await h.owner()
        created = await h.register(owner)
        plans.features.append("camera")
        assert (await h.client.post(_url(created["id"]), headers=owner.headers)).status_code == 200
        plans.features.remove("camera")
        response = await h.client.post(_url(created["id"]), headers=owner.headers)
        assert response.status_code == 403
        problem = response.json()
        assert problem["type"] == "urn:mowdeck:problem:feature-not-in-plan"
        assert problem["errors"][0]["parameter"] == "camera"
        assert len(fake.requests) == 1


async def test_fleet_camera_session_maps_gateway_errors() -> None:
    bus = InMemoryBus()
    async with harness(bus=bus, camera_request_timeout_seconds=1) as h:
        fake = await _gateway(bus)
        owner = await h.owner()
        created = await h.register(owner)
        url = _url(created["id"])

        def error(code: CameraSessionErrorCode) -> CameraSessionReply:
            return CameraSessionReply(error=CameraSessionError(code=code, detail=f"{code} here"))

        fake.reply = error(CameraSessionErrorCode.UNSUPPORTED)
        response = await h.client.post(url, headers=owner.headers)
        assert response.status_code == 409
        assert response.json()["type"] == "urn:mowdeck:problem:camera-unsupported"
        fake.reply = error(CameraSessionErrorCode.UNAVAILABLE)
        assert (await h.client.post(url, headers=owner.headers)).status_code == 503
        fake.reply = error(CameraSessionErrorCode.NOT_FOUND)
        assert (await h.client.post(url, headers=owner.headers)).status_code == 503
        fake.reply = error(CameraSessionErrorCode.FORBIDDEN)
        assert (await h.client.post(url, headers=owner.headers)).status_code == 404
        fake.reply = None  # the gateway stays silent
        response = await h.client.post(url, headers=owner.headers)
        assert response.status_code == 503
        assert response.json()["type"] == "urn:mowdeck:problem:unavailable"
        async with h.store.transaction() as tx:
            assert await tx.audit.find(action=CAMERA_OPENED) == []


async def test_fleet_camera_session_without_any_gateway_is_unavailable() -> None:
    async with harness() as h:
        owner = await h.owner()
        created = await h.register(owner)
        response = await h.client.post(_url(created["id"]), headers=owner.headers)
        assert response.status_code == 503
