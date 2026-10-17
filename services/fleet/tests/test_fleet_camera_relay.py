"""Fleet as the front door of the camera relay (ADR 0045): transport selection and fallback,
error mapping, renew and detach with account separation, and the audit of the relay's events."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fleet_support import API, Harness, harness
from mowdeck_contracts.v1 import (
    CameraProvider,
    CameraSession,
    CameraSessionReply,
    CameraSessionRequest,
    EventEnvelope,
    PrincipalKind,
    PrincipalRef,
    RelayEndpoint,
    RelayTile,
)
from mowdeck_contracts.v1.relay import (
    CAMERA_RELAY_LEASE_OPENED,
    CAMERA_RELAY_STARTED,
    CAMERA_RELAY_STOPPED,
    CameraRelayLeaseOpened,
    CameraRelayStarted,
    CameraRelayStopped,
    RelayAttachReply,
    RelayAttachRequest,
    RelayDetachReply,
    RelayDetachRequest,
    RelayError,
    RelayErrorCode,
    RelayPurpose,
    RelayRenewReply,
    RelayRenewRequest,
    RelayStopReason,
)
from mowdeck_contracts.v1.subjects import (
    ALL_CAMERA_SESSIONS,
    RELAY_CAMERA_ATTACH,
    RELAY_CAMERA_DETACH,
    RELAY_CAMERA_RENEW,
    camera_relay_event_subject,
)
from mowdeck_service_kit.bus import BusMessage, InMemoryBus
from mowdeck_service_kit.testing import FakeEntitlements

from fleet.application.camera import (
    CAMERA_OPENED,
    CAMERA_STREAM_STARTED,
    CAMERA_STREAM_STOPPED,
    RELAY_SERVICE_ACTOR,
)

TICKET = "v1.relay-ticket.sig"  # a test value, never a real ticket
VENDOR_TOKEN = "fake-token-123"  # noqa: S105 - a test value


def _relay_session(lease_id: UUID | None = None) -> CameraSession:
    return CameraSession(
        provider=CameraProvider.RELAY,
        app_id="",
        channel="",
        token="",
        uid=0,
        expires_at=datetime.now(UTC) + timedelta(seconds=60),
        relay=RelayEndpoint(
            tiles=[
                RelayTile(camera_id="front_left", whep_url="/media/whep/r/abc/front_left/whep"),
                RelayTile(camera_id="front_right", whep_url="/media/whep/r/abc/front_right/whep"),
            ],
            ticket=TICKET,
            lease_id=lease_id or uuid4(),
        ),
    )


def _direct_session() -> CameraSession:
    return CameraSession(
        provider=CameraProvider.FAKE,
        app_id="",
        channel="sim",
        token=VENDOR_TOKEN,
        uid=0,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )


def _error(code: RelayErrorCode) -> RelayError:
    return RelayError(code=code, detail=f"relay says {code.value}")


class FakeRelay:
    """Answers ``relay.camera.*`` like camera-relay; a reply of None stays silent."""

    def __init__(self) -> None:
        self.lease_id = uuid4()
        self.attach_reply: RelayAttachReply | None = RelayAttachReply(
            session=_relay_session(self.lease_id)
        )
        self.renew_reply: RelayRenewReply | None = RelayRenewReply(
            session=_relay_session(self.lease_id)
        )
        self.detach_reply: RelayDetachReply | None = RelayDetachReply(detached=True)
        self.attaches: list[RelayAttachRequest] = []
        self.renews: list[RelayRenewRequest] = []
        self.detaches: list[RelayDetachRequest] = []

    async def on_attach(self, message: BusMessage) -> None:
        self.attaches.append(message.model(RelayAttachRequest))
        if self.attach_reply is not None:
            await message.respond(self.attach_reply)

    async def on_renew(self, message: BusMessage) -> None:
        self.renews.append(message.model(RelayRenewRequest))
        if self.renew_reply is not None:
            await message.respond(self.renew_reply)

    async def on_detach(self, message: BusMessage) -> None:
        self.detaches.append(message.model(RelayDetachRequest))
        if self.detach_reply is not None:
            await message.respond(self.detach_reply)


class FakeGateway:
    def __init__(self) -> None:
        self.requests: list[CameraSessionRequest] = []

    async def handle(self, message: BusMessage) -> None:
        self.requests.append(message.model(CameraSessionRequest))
        await message.respond(CameraSessionReply(session=_direct_session()))


async def _fakes(bus: InMemoryBus, *, relay: bool = True) -> tuple[FakeRelay, FakeGateway]:
    fake_relay, gateway = FakeRelay(), FakeGateway()
    await bus.subscribe(ALL_CAMERA_SESSIONS, gateway.handle)
    if relay:
        await bus.subscribe(RELAY_CAMERA_ATTACH, fake_relay.on_attach, queue="camera-relay")
        await bus.subscribe(RELAY_CAMERA_RENEW, fake_relay.on_renew, queue="camera-relay")
        await bus.subscribe(RELAY_CAMERA_DETACH, fake_relay.on_detach, queue="camera-relay")
    return fake_relay, gateway


def _url(mower_id: str | UUID, lease_id: UUID | None = None, suffix: str = "") -> str:
    base = f"{API}/mowers/{mower_id}/camera/session"
    return base if lease_id is None else f"{base}/{lease_id}{suffix}"


async def _opened_rows(h: Harness) -> list[Any]:
    async with h.store.transaction() as tx:
        return await tx.audit.find(action=CAMERA_OPENED)


# --- Transport selection ----------------------------------------------------------------------


async def test_auto_without_a_body_uses_the_relay_and_returns_its_session_unchanged() -> None:
    bus = InMemoryBus()
    async with harness(bus=bus) as h:
        relay, gateway = await _fakes(bus)
        owner = await h.owner()
        viewer = await h.member(owner, "viewer")
        created = await h.register(owner)
        response = await h.client.post(_url(created["id"]), headers=viewer.headers)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        body: dict[str, Any] = response.json()
        assert body == _relay_session(relay.lease_id).model_dump(mode="json") | {
            "expires_at": body["expires_at"]
        }
        assert body["relay"]["ticket"] == TICKET
        assert gateway.requests == []  # no vendor session for the browser
        (request,) = relay.attaches
        assert request.account_id == owner.account_id  # from the token, never the client
        assert str(request.mower_id) == created["id"]
        assert request.camera_ids == []  # every camera the mower offers
        assert request.purpose is RelayPurpose.VIEWER
        assert request.requested_by.subject == viewer.principal.subject
        (row,) = await _opened_rows(h)
        assert row.details["transport"] == "relay"
        assert row.details["requested_transport"] == "auto"
        assert row.details["provider"] == "relay"
        assert row.details["lease_id"] == str(relay.lease_id)
        assert row.details["cameras"] == 2
        assert TICKET not in str(row.details)


@pytest.mark.parametrize("why", ["unsupported_provider", "no_responder", "timeout"])
async def test_auto_falls_back_to_direct_when_the_relay_cannot_serve_or_is_not_deployed(
    why: str,
) -> None:
    bus = InMemoryBus()
    async with harness(bus=bus, camera_relay_timeout_seconds=1) as h:
        relay, gateway = await _fakes(bus, relay=why != "no_responder")
        if why == "unsupported_provider":
            relay.attach_reply = RelayAttachReply(error=_error(RelayErrorCode.UNSUPPORTED_PROVIDER))
        elif why == "timeout":
            relay.attach_reply = None
        owner = await h.owner()
        created = await h.register(owner)
        response = await h.client.post(_url(created["id"]), headers=owner.headers)
        assert response.status_code == 200, response.text
        assert response.json()["provider"] == "fake"
        assert response.json()["token"] == VENDOR_TOKEN
        assert len(gateway.requests) == 1
        (row,) = await _opened_rows(h)
        assert (row.details["transport"], row.details["requested_transport"]) == (
            "direct",
            "auto",
        )
        assert VENDOR_TOKEN not in str(row.details)


@pytest.mark.parametrize(
    ("code", "status", "problem"),
    [
        (RelayErrorCode.FORBIDDEN, 404, "not-found"),
        (RelayErrorCode.NOT_FOUND, 404, "not-found"),
        (RelayErrorCode.BUDGET, 409, "camera-budget"),
        (RelayErrorCode.UNAVAILABLE, 503, "unavailable"),
    ],
)
@pytest.mark.parametrize("transport", ["auto", "relay"])
async def test_relay_errors_are_answered_and_never_fall_back(
    code: RelayErrorCode, status: int, problem: str, transport: str
) -> None:
    bus = InMemoryBus()
    async with harness(bus=bus) as h:
        relay, gateway = await _fakes(bus)
        relay.attach_reply = RelayAttachReply(error=_error(code))
        owner = await h.owner()
        created = await h.register(owner)
        response = await h.client.post(
            _url(created["id"]), headers=owner.headers, json={"transport": transport}
        )
        assert response.status_code == status, response.text
        assert response.json()["type"] == f"urn:mowdeck:problem:{problem}"
        assert gateway.requests == []
        assert await _opened_rows(h) == []


@pytest.mark.parametrize("why", ["no_responder", "timeout"])
async def test_relay_only_is_503_when_the_relay_is_not_deployed_or_silent(why: str) -> None:
    bus = InMemoryBus()
    async with harness(bus=bus, camera_relay_timeout_seconds=1) as h:
        relay, gateway = await _fakes(bus, relay=why == "timeout")
        relay.attach_reply = None
        owner = await h.owner()
        created = await h.register(owner)
        response = await h.client.post(
            _url(created["id"]), headers=owner.headers, json={"transport": "relay"}
        )
        assert response.status_code == 503, response.text
        assert response.json()["type"] == "urn:mowdeck:problem:unavailable"
        assert gateway.requests == []


async def test_relay_only_with_an_unsupported_provider_is_409_without_fallback() -> None:
    bus = InMemoryBus()
    async with harness(bus=bus) as h:
        relay, gateway = await _fakes(bus)
        relay.attach_reply = RelayAttachReply(error=_error(RelayErrorCode.UNSUPPORTED_PROVIDER))
        owner = await h.owner()
        created = await h.register(owner)
        response = await h.client.post(
            _url(created["id"]), headers=owner.headers, json={"transport": "relay"}
        )
        assert response.status_code == 409
        assert response.json()["type"] == "urn:mowdeck:problem:camera-relay-unsupported"
        assert gateway.requests == []


async def test_direct_never_asks_the_relay() -> None:
    bus = InMemoryBus()
    async with harness(bus=bus) as h:
        relay, gateway = await _fakes(bus)
        owner = await h.owner()
        created = await h.register(owner)
        response = await h.client.post(
            _url(created["id"]), headers=owner.headers, json={"transport": "direct"}
        )
        assert response.status_code == 200
        assert response.json()["provider"] == "fake"
        assert relay.attaches == [] and len(gateway.requests) == 1
        (row,) = await _opened_rows(h)
        assert (row.details["transport"], row.details["requested_transport"]) == (
            "direct",
            "direct",
        )


@pytest.mark.parametrize(
    "body", [{"transport": "webrtc"}, {"transport": "relay", "account_id": str(uuid4())}]
)
async def test_the_body_is_validated(body: dict[str, Any]) -> None:
    bus = InMemoryBus()
    async with harness(bus=bus) as h:
        relay, gateway = await _fakes(bus)
        owner = await h.owner()
        created = await h.register(owner)
        response = await h.client.post(_url(created["id"]), headers=owner.headers, json=body)
        assert response.status_code == 422
        assert relay.attaches == [] and gateway.requests == []


async def test_a_foreign_mower_is_hidden_before_the_relay_is_asked() -> None:
    bus = InMemoryBus()
    async with harness(bus=bus) as h:
        relay, gateway = await _fakes(bus)
        owner = await h.owner()
        stranger = await h.owner()
        created = await h.register(owner)
        response = await h.client.post(
            _url(created["id"]), headers=stranger.headers, json={"transport": "relay"}
        )
        assert response.status_code == 404
        assert relay.attaches == [] and gateway.requests == []


async def test_the_relay_path_needs_the_plan_feature() -> None:
    bus = InMemoryBus()
    plans = FakeEntitlements(plan="crafted", features=["view", "control"])
    async with harness(bus=bus, entitlements=plans) as h:
        relay, _ = await _fakes(bus)
        owner = await h.owner()
        created = await h.register(owner)
        response = await h.client.post(
            _url(created["id"]), headers=owner.headers, json={"transport": "relay"}
        )
        assert response.status_code == 403
        assert response.json()["type"] == "urn:mowdeck:problem:feature-not-in-plan"
        renew = await h.client.post(_url(created["id"], uuid4(), "/renew"), headers=owner.headers)
        assert renew.status_code == 403
        # Ending a stream is always allowed.
        detach = await h.client.delete(_url(created["id"], uuid4()), headers=owner.headers)
        assert detach.status_code == 204
        assert relay.attaches == [] and relay.renews == [] and len(relay.detaches) == 1


# --- Renew and detach -------------------------------------------------------------------------


async def test_renew_answers_the_relay_session_with_a_fresh_ticket() -> None:
    bus = InMemoryBus()
    async with harness(bus=bus) as h:
        relay, _ = await _fakes(bus)
        owner = await h.owner()
        viewer = await h.member(owner, "viewer")
        created = await h.register(owner)
        response = await h.client.post(
            _url(created["id"], relay.lease_id, "/renew"), headers=viewer.headers
        )
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["relay"]["lease_id"] == str(relay.lease_id)
        (request,) = relay.renews
        assert request.account_id == owner.account_id
        assert str(request.mower_id) == created["id"]
        assert request.lease_id == relay.lease_id
        assert request.requested_by.subject == viewer.principal.subject


@pytest.mark.parametrize("code", [RelayErrorCode.NOT_FOUND, RelayErrorCode.FORBIDDEN])
async def test_renew_of_an_unknown_or_foreign_lease_is_404(code: RelayErrorCode) -> None:
    bus = InMemoryBus()
    async with harness(bus=bus) as h:
        relay, _ = await _fakes(bus)
        relay.renew_reply = RelayRenewReply(error=_error(code))
        owner = await h.owner()
        created = await h.register(owner)
        response = await h.client.post(
            _url(created["id"], uuid4(), "/renew"), headers=owner.headers
        )
        assert response.status_code == 404
        assert response.json()["type"] == "urn:mowdeck:problem:not-found"


async def test_renew_and_detach_of_another_accounts_mower_are_404_without_asking() -> None:
    bus = InMemoryBus()
    async with harness(bus=bus) as h:
        relay, _ = await _fakes(bus)
        owner = await h.owner()
        stranger = await h.owner()
        created = await h.register(owner)
        renew = await h.client.post(
            _url(created["id"], relay.lease_id, "/renew"), headers=stranger.headers
        )
        detach = await h.client.delete(
            _url(created["id"], relay.lease_id), headers=stranger.headers
        )
        assert (renew.status_code, detach.status_code) == (404, 404)
        assert relay.renews == [] and relay.detaches == []


async def test_renew_without_a_relay_is_503() -> None:
    bus = InMemoryBus()
    async with harness(bus=bus) as h:
        await _fakes(bus, relay=False)
        owner = await h.owner()
        created = await h.register(owner)
        response = await h.client.post(
            _url(created["id"], uuid4(), "/renew"), headers=owner.headers
        )
        assert response.status_code == 503


async def test_detach_is_idempotent() -> None:
    bus = InMemoryBus()
    async with harness(bus=bus) as h:
        relay, _ = await _fakes(bus)
        owner = await h.owner()
        created = await h.register(owner)
        url = _url(created["id"], relay.lease_id)
        first = await h.client.delete(url, headers=owner.headers)
        assert first.status_code == 204 and first.content == b""
        relay.detach_reply = RelayDetachReply(detached=False)  # already closed or unknown
        again = await h.client.delete(url, headers=owner.headers)
        assert again.status_code == 204
        unknown = await h.client.delete(_url(created["id"], uuid4()), headers=owner.headers)
        assert unknown.status_code == 204
        (request, *_) = relay.detaches
        assert request.account_id == owner.account_id
        assert str(request.mower_id) == created["id"]
        assert request.lease_id == relay.lease_id


async def test_detach_of_a_lease_of_another_account_is_404() -> None:
    bus = InMemoryBus()
    async with harness(bus=bus) as h:
        relay, _ = await _fakes(bus)
        relay.detach_reply = RelayDetachReply(
            detached=False, error=_error(RelayErrorCode.FORBIDDEN)
        )
        owner = await h.owner()
        created = await h.register(owner)
        response = await h.client.delete(_url(created["id"], uuid4()), headers=owner.headers)
        assert response.status_code == 404


async def test_detach_without_a_relay_is_204_and_a_silent_relay_503() -> None:
    bus = InMemoryBus()
    async with harness(bus=bus, camera_relay_timeout_seconds=1) as h:
        await _fakes(bus, relay=False)
        owner = await h.owner()
        created = await h.register(owner)
        response = await h.client.delete(_url(created["id"], uuid4()), headers=owner.headers)
        assert response.status_code == 204
        silent = FakeRelay()
        silent.detach_reply = None
        await bus.subscribe(RELAY_CAMERA_DETACH, silent.on_detach)
        response = await h.client.delete(_url(created["id"], uuid4()), headers=owner.headers)
        assert response.status_code == 503


# --- Audit of the relay's events --------------------------------------------------------------

T0 = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


def _started(account_id: UUID, mower_id: UUID, by: PrincipalRef | None) -> EventEnvelope[Any]:
    payload = CameraRelayStarted(
        relay_session_id=uuid4(),
        account_id=account_id,
        mower_id=mower_id,
        source=CameraProvider.FAKE,
        camera_ids=["front_left", "front_right"],
        started_at=T0,
        trigger=RelayPurpose.VIEWER,
        started_by=by,
    )
    return EventEnvelope[Any](
        type=CAMERA_RELAY_STARTED,
        account_id=account_id,
        producer="camera-relay",
        payload=payload.model_dump(mode="json"),
    )


def _stopped(account_id: UUID, mower_id: UUID) -> EventEnvelope[Any]:
    payload = CameraRelayStopped(
        relay_session_id=uuid4(),
        account_id=account_id,
        mower_id=mower_id,
        reason=RelayStopReason.IDLE,
        started_at=T0,
        stopped_at=T0 + timedelta(seconds=95.5),
        trigger=RelayPurpose.VIEWER,
        peak_viewers=3,
    )
    return EventEnvelope[Any](
        type=CAMERA_RELAY_STOPPED,
        account_id=account_id,
        producer="camera-relay",
        payload=payload.model_dump(mode="json"),
    )


async def _stream_rows(h: Harness) -> list[Any]:
    async with h.store.transaction() as tx:
        started = await tx.audit.find(action=CAMERA_STREAM_STARTED)
        stopped = await tx.audit.find(action=CAMERA_STREAM_STOPPED)
    return started + stopped


async def test_stream_events_become_audit_rows_once_per_event() -> None:
    bus = InMemoryBus()
    async with harness(bus=bus) as h:
        owner = await h.owner()
        created = await h.register(owner)
        mower_id = UUID(created["id"])
        by = PrincipalRef(subject=owner.principal.subject, kind=PrincipalKind.USER)
        started = _started(owner.account_id, mower_id, by)
        stopped = _stopped(owner.account_id, mower_id)
        for _ in range(2):  # a redelivery writes nothing
            await bus.publish(camera_relay_event_subject(mower_id, "started"), started)
            await bus.publish(camera_relay_event_subject(mower_id, "stopped"), stopped)
        first, second = await _stream_rows(h)
        assert (first.action, first.account_id, first.target_id) == (
            CAMERA_STREAM_STARTED,
            owner.account_id,
            created["id"],
        )
        assert first.actor_subject == f"user:{owner.principal.subject}"
        assert first.details["trigger"] == "viewer"
        assert first.details["source"] == "fake"
        assert first.event_id == started.id
        assert first.at == T0
        assert second.action == CAMERA_STREAM_STOPPED
        assert second.actor_subject == RELAY_SERVICE_ACTOR
        assert second.details["reason"] == "idle"
        assert second.details["trigger"] == "viewer"
        assert second.details["duration_seconds"] == 95.5
        assert second.details["peak_viewers"] == 3
        assert second.event_id == stopped.id
        # The service itself is idempotent too (a delivery racing another one).
        assert await h.app.state.fleet.camera.record_relay_event(stopped) is False


async def test_lease_events_are_ignored_and_invalid_events_dropped() -> None:
    bus = InMemoryBus()
    async with harness(bus=bus) as h:
        owner = await h.owner()
        created = await h.register(owner)
        mower_id = UUID(created["id"])
        opened = EventEnvelope[Any](
            type=CAMERA_RELAY_LEASE_OPENED,
            account_id=owner.account_id,
            producer="camera-relay",
            payload=CameraRelayLeaseOpened(
                lease_id=uuid4(),
                relay_session_id=uuid4(),
                account_id=owner.account_id,
                mower_id=mower_id,
                purpose=RelayPurpose.VIEWER,
                requested_by=PrincipalRef(subject=uuid4(), kind=PrincipalKind.USER),
                camera_ids=["front_left"],
                opened_at=T0,
            ).model_dump(mode="json"),
        )
        await bus.publish(camera_relay_event_subject(mower_id, "lease_opened"), opened)
        broken = _stopped(owner.account_id, mower_id).model_copy(update={"payload": {"x": 1}})
        await bus.publish(camera_relay_event_subject(mower_id, "stopped"), broken)
        foreign = _stopped(owner.account_id, mower_id).model_copy(update={"account_id": uuid4()})
        await bus.publish(camera_relay_event_subject(mower_id, "stopped"), foreign)
        assert await _stream_rows(h) == []
