"""``POST /mowers/{id}/path-preview``: checks, relay to the gateway, error mapping."""

from datetime import UTC, datetime
from uuid import UUID

from fleet_support import API, harness
from mowdeck_contracts.v1 import (
    MowPath,
    PathPreviewError,
    PathPreviewErrorCode,
    PathPreviewGatewayRequest,
    PathPreviewReply,
)
from mowdeck_contracts.v1.subjects import ALL_PATH_PREVIEWS
from mowdeck_service_kit.bus import BusMessage, InMemoryBus
from mowdeck_service_kit.testing import FakeEntitlements

BODY = {"area_ids": ["front", "back"], "pattern": {"name": "stripes", "angle_deg": 30}}


def _path() -> MowPath:
    return MowPath(
        path_hash="18446744073709551557",
        polylines=[[(0.0, 0.0), (4.0, 0.0), (4.0, 0.5)]],
        kinds=["mow"],
        points_total=3,
        area_ids=["front"],
        planned_at=datetime(2026, 10, 11, 9, tzinfo=UTC),
    )


class FakePreviewGateway:
    """Answers preview requests like the Device-Gateway; ``reply`` None stays silent."""

    def __init__(self) -> None:
        self.reply: PathPreviewReply | None = PathPreviewReply(path=_path())
        self.requests: list[PathPreviewGatewayRequest] = []

    async def handle(self, message: BusMessage) -> None:
        self.requests.append(message.model(PathPreviewGatewayRequest))
        if self.reply is not None:
            await message.respond(self.reply)


async def _gateway(bus: InMemoryBus) -> FakePreviewGateway:
    fake = FakePreviewGateway()
    await bus.subscribe(ALL_PATH_PREVIEWS, fake.handle)
    return fake


def _url(mower_id: str | UUID) -> str:
    return f"{API}/mowers/{mower_id}/path-preview"


async def test_fleet_path_preview_is_relayed_and_not_cached() -> None:
    bus = InMemoryBus()
    async with harness(bus=bus) as h:
        fake = await _gateway(bus)
        owner = await h.owner()
        operator = await h.member(owner, "operator")
        created = await h.register(owner)
        response = await h.client.post(_url(created["id"]), json=BODY, headers=operator.headers)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        body = response.json()
        assert body["path_hash"] == "18446744073709551557"
        assert body["points_total"] == 3
        (request,) = fake.requests
        assert str(request.mower_id) == created["id"]
        assert request.account_id == owner.account_id
        assert request.area_ids == ["front", "back"]
        assert request.pattern == {"name": "stripes", "angle_deg": 30}
        assert request.requested_by.subject == operator.principal.subject


async def test_fleet_path_preview_needs_control_and_hides_foreign_mowers() -> None:
    bus = InMemoryBus()
    async with harness(bus=bus) as h:
        fake = await _gateway(bus)
        owner = await h.owner()
        viewer = await h.member(owner, "viewer")
        stranger = await h.owner()
        created = await h.register(owner)
        seen = await h.client.post(_url(created["id"]), json=BODY, headers=viewer.headers)
        hidden = await h.client.post(_url(created["id"]), json=BODY, headers=stranger.headers)
        empty = await h.client.post(
            _url(created["id"]), json={"area_ids": []}, headers=owner.headers
        )
    assert seen.status_code == 403
    assert hidden.status_code == 404
    assert empty.status_code == 422
    assert fake.requests == []


async def test_fleet_path_preview_needs_the_control_feature() -> None:
    bus = InMemoryBus()
    plans = FakeEntitlements(plan="crafted", features=["view", "emergency_stop"])
    async with harness(bus=bus, entitlements=plans) as h:
        fake = await _gateway(bus)
        owner = await h.owner()
        created = await h.register(owner)
        response = await h.client.post(_url(created["id"]), json=BODY, headers=owner.headers)
    assert response.status_code == 403
    assert response.json()["type"] == "urn:mowdeck:problem:feature-not-in-plan"
    assert fake.requests == []


async def test_fleet_path_preview_maps_gateway_errors() -> None:
    bus = InMemoryBus()
    async with harness(bus=bus, path_preview_request_timeout_seconds=5) as h:
        fake = await _gateway(bus)
        owner = await h.owner()
        created = await h.register(owner)
        url = _url(created["id"])

        def error(code: PathPreviewErrorCode) -> PathPreviewReply:
            return PathPreviewReply(error=PathPreviewError(code=code, detail=f"{code} here"))

        expected = {
            PathPreviewErrorCode.UNSUPPORTED: (409, "path-preview-unsupported"),
            PathPreviewErrorCode.REJECTED: (409, "path-preview-rejected"),
            PathPreviewErrorCode.TIMEOUT: (504, "gateway-timeout"),
            PathPreviewErrorCode.UNAVAILABLE: (503, "unavailable"),
            PathPreviewErrorCode.NOT_FOUND: (503, "unavailable"),
            PathPreviewErrorCode.FAILED: (503, "unavailable"),
            PathPreviewErrorCode.FORBIDDEN: (404, "not-found"),
        }
        for code, (status, problem) in expected.items():
            fake.reply = error(code)
            response = await h.client.post(url, json=BODY, headers=owner.headers)
            assert response.status_code == status, (code, response.text)
            assert response.json()["type"] == f"urn:mowdeck:problem:{problem}"
        rejected = error(PathPreviewErrorCode.REJECTED)
        fake.reply = rejected
        response = await h.client.post(url, json=BODY, headers=owner.headers)
        assert response.json()["detail"] == "rejected here"


async def test_fleet_path_preview_times_out_when_the_gateway_is_silent() -> None:
    bus = InMemoryBus()
    async with harness(bus=bus, path_preview_request_timeout_seconds=5) as h:
        fake = await _gateway(bus)
        fake.reply = None
        owner = await h.owner()
        created = await h.register(owner)
        h.app.state.fleet.path_preview.gateway.timeout_seconds = 0.05
        response = await h.client.post(_url(created["id"]), json=BODY, headers=owner.headers)
    assert response.status_code == 504
    assert response.json()["type"] == "urn:mowdeck:problem:gateway-timeout"


async def test_fleet_path_preview_without_any_gateway_is_unavailable() -> None:
    async with harness() as h:
        owner = await h.owner()
        created = await h.register(owner)
        response = await h.client.post(_url(created["id"]), json=BODY, headers=owner.headers)
    assert response.status_code == 503
