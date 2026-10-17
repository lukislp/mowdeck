"""The reconcile loop (media server readers and paths -> use cases), kicks and the gateway port."""

import asyncio
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from mowdeck_contracts.v1 import (
    CameraReleaseReply,
    CameraReleaseRequest,
    CameraSession,
    CameraSessionReply,
    CameraSessionRequest,
)
from mowdeck_contracts.v1.camera import (
    CameraInfo,
    CameraProvider,
    CameraSessionError,
    CameraSessionErrorCode,
)
from mowdeck_contracts.v1.principal import PrincipalKind
from mowdeck_contracts.v1.relay import RelayDetachRequest, RelayErrorCode
from mowdeck_contracts.v1.subjects import ALL_CAMERA_RELEASES, ALL_CAMERA_SESSIONS
from mowdeck_service_kit.bus import BusMessage, InMemoryBus
from relay_support import T0, Harness, attach_request, key

from camera_relay.adapters.gateway import (
    OFFER_REUSE_SECONDS,
    GatewayVendorSessions,
    service_principal,
)
from camera_relay.adapters.media.mediamtx import (
    MediaMtxApi,
    MediaMtxPlane,
    MediaPath,
    WebRtcSession,
)
from camera_relay.adapters.media.pipeline import RunningSource, viewer_path
from camera_relay.adapters.media.reconcile import Reconciler
from camera_relay.application.media_auth import WhepBindings
from camera_relay.application.ports import (
    ForbiddenSourceError,
    SourceUnavailableError,
    UnsupportedProviderError,
)
from camera_relay.domain.effects import StartVendorSession, StopVendorSession, UpdateCameras
from camera_relay.domain.models import SessionState

RID = "0123456789abcdef0123456789abcdef"


@dataclass
class FakeApi:
    """The control API as data."""

    sessions: list[WebRtcSession] = field(default_factory=list)
    ready: set[str] = field(default_factory=set)
    kicked: list[str] = field(default_factory=list)

    async def webrtc_sessions(self) -> list[WebRtcSession]:
        return list(self.sessions)

    async def paths(self) -> list[MediaPath]:
        return [MediaPath(name, True) for name in self.ready]

    async def kick_webrtc_session(self, session_id: str) -> bool:
        self.kicked.append(session_id)
        self.sessions = [s for s in self.sessions if s.id != session_id]
        return True


@dataclass
class FakeReports:
    confirmed: list[tuple[UUID, str]] = field(default_factory=list)
    cameras: list[str] = field(default_factory=list)
    lost: list[tuple[UUID, str | None]] = field(default_factory=list)
    leases: set[UUID] = field(default_factory=set)
    started: list[tuple[UUID, list[str]]] = field(default_factory=list)

    async def media_confirmed(self, lease_id: UUID, whep_session: str, camera: str = "") -> bool:
        self.confirmed.append((lease_id, whep_session))
        self.cameras.append(camera)
        return lease_id in self.leases

    async def media_lost(self, lease_id: UUID, whep_session: str | None = None) -> bool:
        self.lost.append((lease_id, whep_session))
        return True

    async def source_started(
        self, key: Any, session_id: UUID, source: CameraProvider, publishing: list[str]
    ) -> None:
        self.started.append((session_id, publishing))

    async def source_failed(self, key: Any, session_id: UUID) -> None:
        return None


def _vendor(reports: FakeReports, bus: InMemoryBus | None = None) -> GatewayVendorSessions:
    return GatewayVendorSessions(
        lambda: bus,
        [],
        reports=lambda: reports,
        principal=service_principal(uuid4()),
    )


def _reconciler(api: FakeApi, reports: FakeReports, bindings: WhepBindings) -> Reconciler:
    return Reconciler(
        api=api,  # type: ignore[arg-type]
        bindings=bindings,
        vendor=_vendor(reports),
        reports=lambda: reports,
        stall_polls=2,
    )


def _reader(session_id: str, sent: int, path: str = f"r/{RID}/front_left") -> WebRtcSession:
    return WebRtcSession(id=session_id, path=path, state="read", bytes_sent=sent, established=True)


async def test_growing_bytes_confirm_a_bound_reader_once() -> None:
    api, reports, bindings = FakeApi(), FakeReports(), WhepBindings()
    lease = uuid4()
    reports.leases.add(lease)
    bindings.bind("w1", lease)
    reconciler = _reconciler(api, reports, bindings)
    api.sessions = [_reader("w1", 1000)]
    await reconciler.run_once()
    api.sessions = [_reader("w1", 5000)]
    await reconciler.run_once()
    assert reports.confirmed == [(lease, "w1")]
    assert reports.cameras == ["front_left"]  # the camera of the reader's path
    assert api.kicked == []


async def test_a_stalled_reader_is_lost_and_confirmed_again_when_bytes_flow() -> None:
    api, reports, bindings = FakeApi(), FakeReports(), WhepBindings()
    lease = uuid4()
    reports.leases.add(lease)
    bindings.bind("w1", lease)
    reconciler = _reconciler(api, reports, bindings)
    for sent in (1000, 1000, 1000):
        api.sessions = [_reader("w1", sent)]
        await reconciler.run_once()
    assert reports.lost == [(lease, "w1")]
    api.sessions = [_reader("w1", 2000)]
    await reconciler.run_once()
    assert reports.confirmed == [(lease, "w1"), (lease, "w1")]


async def test_an_unbound_reader_is_kicked() -> None:
    api, reports, bindings = FakeApi(), FakeReports(), WhepBindings()
    api.sessions = [_reader("intruder", 100)]
    await _reconciler(api, reports, bindings).run_once()
    assert api.kicked == ["intruder"]
    assert reports.confirmed == []


async def test_a_reader_of_a_closed_lease_is_kicked() -> None:
    api, reports, bindings = FakeApi(), FakeReports(), WhepBindings()
    bindings.bind("w1", uuid4())  # not in reports.leases: the lease is gone
    api.sessions = [_reader("w1", 100)]
    await _reconciler(api, reports, bindings).run_once()
    assert api.kicked == ["w1"]
    assert bindings.lease_of("w1") is None


async def test_a_gone_confirmed_reader_is_lost_and_unbound() -> None:
    api, reports, bindings = FakeApi(), FakeReports(), WhepBindings()
    lease = uuid4()
    reports.leases.add(lease)
    bindings.bind("w1", lease)
    reconciler = _reconciler(api, reports, bindings)
    api.sessions = [_reader("w1", 100)]
    await reconciler.run_once()
    api.sessions = []
    await reconciler.run_once()
    assert reports.lost == [(lease, "w1")]
    assert bindings.lease_of("w1") is None


async def test_a_binding_that_never_played_is_dropped_without_a_loss() -> None:
    """An authorized attempt that ended before it read (no stream yet, ICE failed) must not
    take the confirmed reader of the same lease with it."""
    api, reports, bindings = FakeApi(), FakeReports(), WhepBindings()
    lease = uuid4()
    reports.leases.add(lease)
    bindings.bind("attempt", lease)
    reconciler = _reconciler(api, reports, bindings)
    await reconciler.run_once()
    assert bindings.lease_of("attempt") == lease  # one pass of grace
    await reconciler.run_once()
    assert bindings.lease_of("attempt") is None
    assert reports.lost == []


async def test_other_paths_and_states_are_ignored() -> None:
    api, reports, bindings = FakeApi(), FakeReports(), WhepBindings()
    api.sessions = [
        WebRtcSession("p1", f"r/{RID}/front_left", "publish", 10, True),
        WebRtcSession("x1", "elsewhere", "read", 10, True),
    ]
    await _reconciler(api, reports, bindings).run_once()
    assert api.kicked == []


async def test_a_ready_path_reports_the_source_started_once() -> None:
    api, reports, bindings = FakeApi(), FakeReports(), WhepBindings()
    reconciler = _reconciler(api, reports, bindings)
    source = RunningSource(
        key=key(),
        session_id=uuid4(),
        relay_id=RID,
        provider=CameraProvider.FAKE,
        cameras=("front_left", "front_right"),
    )
    reconciler.vendor.sources[RID] = source
    await reconciler.run_once()
    assert reports.started == []
    api.ready = {viewer_path(RID, "front_left")}
    await reconciler.run_once()
    await reconciler.run_once()
    assert reports.started == [(source.session_id, ["front_left"])]


async def test_media_reports_reach_the_domain_and_detach_kicks_the_bound_reader() -> None:
    h = Harness()
    relay_key = key()
    reply = await h.service.attach(attach_request(relay_key))
    assert reply.session is not None and reply.session.relay is not None
    lease_id = reply.session.relay.lease_id
    session = h.repository.get(relay_key)
    assert session is not None and session.session_id is not None
    await h.service.source_started(relay_key, session.session_id, CameraProvider.FAKE, ["x"])
    assert await h.service.media_confirmed(lease_id, "w1")
    assert session.leases[lease_id].whep_session == "w1"
    # The loss of another reader of the same lease changes nothing.
    await h.service.media_lost(lease_id, "other")
    assert session.leases[lease_id].whep_session == "w1"
    await h.service.detach(
        RelayDetachRequest(
            account_id=relay_key.account_id,
            mower_id=relay_key.mower_id,
            lease_id=lease_id,
            requested_by=attach_request(relay_key).requested_by,
        )
    )
    assert h.media.kicked == ["w1"]
    assert session.state is SessionState.DRAINING


async def test_the_plane_kicks_through_the_api_and_drops_the_binding() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(404 if request.url.path.endswith("/gone") else 200)

    api = MediaMtxApi(
        "http://mediamtx:9997/",
        user="relay",
        secret=lambda: "x",
        transport=httpx.MockTransport(handler),
    )
    bindings = WhepBindings()
    bindings.bind("w1", uuid4())
    plane = MediaMtxPlane(api, bindings)
    await plane.kick("w1")
    await plane.kick("gone")
    assert [(r.method, r.url.path) for r in requests] == [
        ("POST", "/v3/webrtcsessions/kick/w1"),
        ("POST", "/v3/webrtcsessions/kick/gone"),
    ]
    assert bindings.lease_of("w1") is None
    await api.close()


async def test_the_api_client_pages_and_maps_the_lists() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        if request.url.path == "/v3/paths/list":
            return httpx.Response(
                200, json={"pageCount": 1, "items": [{"name": "r/x/y", "ready": True}]}
            )
        item = {
            "id": f"s{page}",
            "path": "r/x/y",
            "state": "read",
            "bytesSent": 10 * (page + 1),
            "peerConnectionEstablished": True,
        }
        return httpx.Response(200, json={"pageCount": 2, "items": [item]})

    api = MediaMtxApi(
        "http://m:9997", user="u", secret=lambda: "s", transport=httpx.MockTransport(handler)
    )
    sessions = await api.webrtc_sessions()
    assert [(s.id, s.bytes_sent) for s in sessions] == [("s0", 10), ("s1", 20)]
    assert await api.paths() == [MediaPath("r/x/y", True)]
    await api.close()


# --- The gateway port --------------------------------------------------------------------------


@dataclass
class FakeDriver:
    provider: CameraProvider = CameraProvider.FAKE
    started: list[RunningSource] = field(default_factory=list)
    updated: list[tuple[str, ...]] = field(default_factory=list)
    stopped: list[RunningSource] = field(default_factory=list)
    fail: bool = False

    async def start(self, source: RunningSource) -> None:
        self.started.append(source)
        if self.fail:
            raise OSError("ffmpeg missing")

    async def update(self, source: RunningSource, cameras: Any) -> None:
        self.updated.append(tuple(cameras))

    async def stop(self, source: RunningSource) -> None:
        self.stopped.append(source)


@dataclass
class Gateway:
    """Answers the camera requests on an in-memory bus."""

    provider: CameraProvider = CameraProvider.FAKE
    error: CameraSessionErrorCode | None = None
    sessions: list[CameraSessionRequest] = field(default_factory=list)
    releases: list[CameraReleaseRequest] = field(default_factory=list)

    async def on_session(self, message: BusMessage) -> None:
        request = message.model(CameraSessionRequest)
        self.sessions.append(request)
        if self.error is not None:
            reply = CameraSessionReply(error=CameraSessionError(code=self.error, detail="no"))
        else:
            reply = CameraSessionReply(
                session=CameraSession(
                    provider=self.provider,
                    app_id="",
                    channel="sim-1",
                    token="",
                    uid=1,
                    cameras=[
                        CameraInfo(id="front_left", name="L"),
                        CameraInfo(id="front_right", name="R"),
                    ],
                    expires_at=T0,
                )
            )
        await message.respond(reply)

    async def on_release(self, message: BusMessage) -> None:
        self.releases.append(message.model(CameraReleaseRequest))
        await message.respond(CameraReleaseReply(released=True))


async def _wired(gateway: Gateway, driver: FakeDriver) -> tuple[GatewayVendorSessions, InMemoryBus]:
    bus = InMemoryBus()
    await bus.subscribe(ALL_CAMERA_SESSIONS, gateway.on_session)
    await bus.subscribe(ALL_CAMERA_RELEASES, gateway.on_release)
    vendor = GatewayVendorSessions(
        lambda: bus,
        [driver],
        reports=lambda: FakeReports(),
        principal=service_principal(uuid4()),
        timeout_seconds=1,
    )
    return vendor, bus


async def test_start_requests_the_session_with_the_service_principal_and_runs_the_driver() -> None:
    gateway, driver = Gateway(), FakeDriver()
    vendor, _ = await _wired(gateway, driver)
    relay_key = key()
    start = StartVendorSession(relay_key, uuid4(), RID, ("front_left", "rear"))
    await vendor.start(start)
    request = gateway.sessions[0]
    assert (request.mower_id, request.account_id) == (relay_key.mower_id, relay_key.account_id)
    assert request.requested_by.kind is PrincipalKind.SERVICE
    assert driver.started[0].cameras == ("front_left",)  # the mower offers no "rear"
    assert vendor.sources[RID].channel == "sim-1"
    await vendor.update_cameras(UpdateCameras(relay_key, start.session_id, RID, ("front_right",)))
    assert driver.updated == [("front_right",)]
    await vendor.stop(StopVendorSession(relay_key, start.session_id, RID, release=True))
    assert driver.stopped == [driver.started[0]]
    assert [(r.mower_id, r.channel) for r in gateway.releases] == [(relay_key.mower_id, "sim-1")]
    assert gateway.releases[0].requested_by.kind is PrincipalKind.SERVICE


async def test_a_provider_without_a_driver_is_unsupported_and_never_released() -> None:
    # ADR 0045: `auto` falls back to the direct path on `unsupported_provider`, and the direct
    # viewer needs the mower's stream: the vendor session is left alone.
    gateway, driver = Gateway(provider=CameraProvider.AGORA), FakeDriver()
    vendor, _ = await _wired(gateway, driver)
    relay_key = key()
    with pytest.raises(UnsupportedProviderError, match="agora"):
        await vendor.offer(relay_key)
    with pytest.raises(UnsupportedProviderError, match="agora"):
        await vendor.start(StartVendorSession(relay_key, uuid4(), RID, ("front_left",)))
    assert UnsupportedProviderError.code is RelayErrorCode.UNSUPPORTED_PROVIDER
    assert driver.started == []
    assert gateway.releases == []
    assert vendor.sources == {}
    assert len(gateway.sessions) == 2  # the refused offer is not reused


async def test_start_reuses_the_offered_session_only_while_it_is_fresh() -> None:
    now = [100.0]
    gateway, driver = Gateway(), FakeDriver()
    vendor, _ = await _wired(gateway, driver)
    vendor._monotonic = lambda: now[0]
    relay_key = key()
    offer = await vendor.offer(relay_key)
    assert offer.provider is CameraProvider.FAKE
    assert [c.id for c in offer.cameras] == ["front_left", "front_right"]
    await vendor.start(StartVendorSession(relay_key, uuid4(), RID, ("front_left",)))
    assert len(gateway.sessions) == 1  # the offered session started the driver
    await vendor.stop(StopVendorSession(relay_key, uuid4(), RID, release=False))
    await vendor.offer(relay_key)
    now[0] += OFFER_REUSE_SECONDS + 1
    await vendor.start(StartVendorSession(relay_key, uuid4(), RID, ("front_left",)))
    assert len(gateway.sessions) == 3  # too old: a fresh session for the start


async def test_discard_releases_an_unused_offer_once() -> None:
    gateway, driver = Gateway(), FakeDriver()
    vendor, _ = await _wired(gateway, driver)
    relay_key = key()
    await vendor.offer(relay_key)
    await vendor.discard(relay_key)
    await vendor.discard(relay_key)  # nothing offered any more
    assert [(r.mower_id, r.channel) for r in gateway.releases] == [(relay_key.mower_id, "sim-1")]


@pytest.mark.parametrize(
    ("code", "error"),
    [
        (CameraSessionErrorCode.FORBIDDEN, ForbiddenSourceError),
        (CameraSessionErrorCode.UNAVAILABLE, SourceUnavailableError),
        (CameraSessionErrorCode.NOT_FOUND, SourceUnavailableError),
    ],
)
async def test_gateway_errors_map_to_relay_errors(
    code: CameraSessionErrorCode, error: type[Exception]
) -> None:
    vendor, _ = await _wired(Gateway(error=code), FakeDriver())
    with pytest.raises(error):
        await vendor.start(StartVendorSession(key(), uuid4(), RID, ("front_left",)))
    assert ForbiddenSourceError.code is RelayErrorCode.FORBIDDEN


async def test_no_gateway_or_no_bus_is_unavailable() -> None:
    reports = FakeReports()
    without_bus = _vendor(reports, None)
    with pytest.raises(SourceUnavailableError, match="bus"):
        await without_bus.start(StartVendorSession(key(), uuid4(), RID, ("front_left",)))
    bus = InMemoryBus()
    silent = _vendor(reports, bus)
    with pytest.raises(SourceUnavailableError, match="did not answer"):
        await silent.start(StartVendorSession(key(), uuid4(), RID, ("front_left",)))


async def test_a_driver_that_cannot_start_releases_the_session() -> None:
    gateway, driver = Gateway(), FakeDriver(fail=True)
    vendor, _ = await _wired(gateway, driver)
    with pytest.raises(SourceUnavailableError, match="OSError"):
        await vendor.start(StartVendorSession(key(), uuid4(), RID, ("front_left",)))
    assert len(driver.stopped) == 1 and len(gateway.releases) == 1
    assert vendor.sources == {}


async def test_a_failure_report_runs_outside_the_caller_and_only_for_running_sources() -> None:
    calls: list[UUID] = []

    @dataclass
    class Reports(FakeReports):
        async def source_failed(self, key: Any, session_id: UUID) -> None:
            calls.append(session_id)

    gateway, driver = Gateway(), FakeDriver()
    vendor, bus = await _wired(gateway, driver)
    vendor._reports = lambda: Reports()
    start = StartVendorSession(key(), uuid4(), RID, ("front_left",))
    await vendor.start(start)
    source = vendor.sources[RID]
    vendor.report_failure(source)
    assert calls == []  # scheduled, not run inside the caller
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert calls == [start.session_id]
    vendor.report_failure(RunningSource(source.key, uuid4(), "e" * 32, CameraProvider.FAKE, ()))
    await asyncio.sleep(0)
    assert calls == [start.session_id]
    del bus
