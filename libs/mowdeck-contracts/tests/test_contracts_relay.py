"""Camera relay contracts (ADR 0045)."""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import (
    CAMERA_RELAY_EVENT_TYPES,
    RELAY_GRACE_SECONDS,
    RELAY_HEARTBEAT_SECONDS,
    RELAY_LEASE_TTL_SECONDS,
    RELAY_TICKET_SECONDS,
    CameraInfo,
    CameraProvider,
    CameraQuota,
    CameraRelayLeaseClosed,
    CameraRelayLeaseOpened,
    CameraRelayStarted,
    CameraRelayStopped,
    CameraSession,
    CameraTransport,
    EventEnvelope,
    PrincipalKind,
    PrincipalRef,
    RelayAttachReply,
    RelayAttachRequest,
    RelayDetachReply,
    RelayDetachRequest,
    RelayEndpoint,
    RelayError,
    RelayErrorCode,
    RelayLeaseCloseReason,
    RelayPurpose,
    RelayRenewReply,
    RelayRenewRequest,
    RelayStopReason,
    RelayTile,
)
from mowdeck_contracts.v1.camera import REDACTED
from mowdeck_contracts.v1.subjects import (
    ALL_CAMERA_RELAY_EVENTS,
    CAMERA,
    CAMERA_RELAY_EVENT_KINDS,
    RELAY_CAMERA_ATTACH,
    RELAY_CAMERA_DETACH,
    RELAY_CAMERA_RENEW,
    RELAY_CAMERA_REQUESTS,
    camera_relay_event_subject,
    parse_camera_relay_event_subject,
    stream_for_subject,
    subject_matches,
)
from pydantic import ValidationError

TICKET = "v1.relay-ticket-secret"
EXPIRES = datetime(2026, 10, 17, 12, 1, tzinfo=UTC)
NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
REF = PrincipalRef(subject=uuid4(), kind=PrincipalKind.USER)


def _endpoint(**changes: object) -> RelayEndpoint:
    values: dict[str, object] = {
        "tiles": [
            RelayTile(camera_id="0", whep_url="/camera/r/3f9a0c7e1b2d4e5f/0/whep"),
            RelayTile(camera_id="1", whep_url="https://mowdeck.example/camera/r/3f9a/1/whep"),
        ],
        "ticket": TICKET,
        "lease_id": uuid4(),
    }
    values.update(changes)
    return RelayEndpoint.model_validate(values)


def _relay_session(**changes: object) -> CameraSession:
    values: dict[str, object] = {
        "provider": CameraProvider.RELAY,
        "app_id": "",
        "channel": "",
        "token": "",
        "uid": 0,
        "cameras": [CameraInfo(id="0", name="Front left"), CameraInfo(id="1", name="Front right")],
        "expires_at": EXPIRES,
        "quota": CameraQuota(used_minutes=3, total_minutes=60),
        "relay": _endpoint(),
    }
    values.update(changes)
    return CameraSession.model_validate(values)


def test_defaults_match_the_adr() -> None:
    assert RELAY_GRACE_SECONDS == 15
    assert RELAY_HEARTBEAT_SECONDS == 15
    assert RELAY_LEASE_TTL_SECONDS == 45
    assert RELAY_TICKET_SECONDS == 60
    assert _endpoint().heartbeat_seconds == RELAY_HEARTBEAT_SECONDS
    assert {t.value for t in CameraTransport} == {"auto", "relay", "direct"}


def test_relay_session_hides_the_ticket_in_repr_and_redacted_dump() -> None:
    session = _relay_session()
    assert TICKET not in repr(session)
    assert TICKET not in repr(session.relay)
    dump = session.redacted()
    assert dump["relay"]["ticket"] == REDACTED
    assert TICKET not in str(dump)
    assert dump["token"] == ""
    assert dump["relay"]["tiles"][0]["camera_id"] == "0"
    # The real dump still carries it for the client.
    again = CameraSession.model_validate_json(session.model_dump_json())
    assert again == session
    assert again.relay is not None
    assert again.relay.ticket == TICKET


def test_relay_session_carries_no_vendor_secrets() -> None:
    with pytest.raises(ValidationError, match="no vendor token"):
        _relay_session(token="vendor")  # noqa: S106 - a test value
    with pytest.raises(ValidationError, match="no vendor token"):
        _relay_session(app_id="app")
    with pytest.raises(ValidationError, match="no vendor token"):
        _relay_session(encryption={"mode": "aes-256-gcm2", "key": "k" * 32})
    with pytest.raises(ValidationError, match="needs a relay endpoint"):
        _relay_session(relay=None)


def test_other_providers_have_no_relay_endpoint() -> None:
    with pytest.raises(ValidationError, match="has no relay endpoint"):
        _relay_session(provider=CameraProvider.FAKE)
    fake = _relay_session(provider=CameraProvider.FAKE, relay=None)
    assert fake.relay is None
    assert fake.redacted()["relay"] is None


def test_relay_endpoint_validation() -> None:
    with pytest.raises(ValidationError):
        _endpoint(tiles=[])
    with pytest.raises(ValidationError):
        _endpoint(ticket="")
    with pytest.raises(ValidationError, match="only one tile"):
        _endpoint(tiles=[{"camera_id": "0", "whep_url": f"/{n}"} for n in "ab"])
    for bad in ("relative/path", "ftp://host/x", "/with space", "javascript:alert(1)"):
        with pytest.raises(ValidationError):
            RelayTile(camera_id="0", whep_url=bad)
    with pytest.raises(ValidationError):
        _endpoint(heartbeat_seconds=1)


def test_attach_request_round_trips_and_validates_camera_ids() -> None:
    request = RelayAttachRequest(
        account_id=uuid4(), mower_id=uuid4(), camera_ids=["0", "1"], requested_by=REF
    )
    assert request.purpose is RelayPurpose.VIEWER
    assert RelayAttachRequest.model_validate_json(request.model_dump_json()) == request
    base = {"account_id": uuid4(), "mower_id": uuid4(), "requested_by": REF}
    every = RelayAttachRequest.model_validate(base)
    assert every.camera_ids == []  # empty: every camera the mower offers
    assert RelayAttachRequest.model_validate({**base, "camera_ids": []}) == every
    for camera_ids in (["0", "0"], [""], ["x" * 65], [str(i) for i in range(9)]):
        with pytest.raises(ValidationError):
            RelayAttachRequest.model_validate({**base, "camera_ids": camera_ids})
    vision = RelayAttachRequest.model_validate({**base, "camera_ids": ["0"], "purpose": "vision"})
    assert vision.purpose is RelayPurpose.VISION


@pytest.mark.parametrize("reply_type", [RelayAttachReply, RelayRenewReply])
def test_session_replies_need_exactly_one_part_and_a_relay_session(
    reply_type: type[RelayAttachReply] | type[RelayRenewReply],
) -> None:
    error = RelayError(code=RelayErrorCode.UNSUPPORTED_PROVIDER, detail="no agora driver")
    assert reply_type(error=error).session is None
    assert reply_type(session=_relay_session()).error is None
    with pytest.raises(ValidationError):
        reply_type()
    with pytest.raises(ValidationError):
        reply_type(session=_relay_session(), error=error)
    with pytest.raises(ValidationError, match="provider relay"):
        reply_type(session=_relay_session(provider=CameraProvider.FAKE, relay=None))
    assert TICKET not in repr(reply_type(session=_relay_session()))


def test_error_codes() -> None:
    assert {c.value for c in RelayErrorCode} == {
        "unsupported_provider",
        "unavailable",
        "budget",
        "forbidden",
        "not_found",
    }


def test_renew_and_detach() -> None:
    ids = {"account_id": uuid4(), "mower_id": uuid4(), "lease_id": uuid4(), "requested_by": REF}
    renew = RelayRenewRequest.model_validate(ids)
    assert RelayRenewRequest.model_validate_json(renew.model_dump_json()) == renew
    detach = RelayDetachRequest.model_validate(ids)
    assert RelayDetachRequest.model_validate_json(detach.model_dump_json()) == detach
    assert RelayDetachReply(detached=True).error is None
    assert not RelayDetachReply(detached=False).detached
    forbidden = RelayError(code=RelayErrorCode.FORBIDDEN, detail="other account")
    assert RelayDetachReply(detached=False, error=forbidden).error == forbidden
    with pytest.raises(ValidationError):
        RelayDetachReply(detached=True, error=forbidden)


def test_events_validate_and_wrap_in_an_envelope() -> None:
    account, mower, session_id, lease = uuid4(), uuid4(), uuid4(), uuid4()
    started = CameraRelayStarted(
        relay_session_id=session_id,
        account_id=account,
        mower_id=mower,
        source=CameraProvider.AGORA,
        camera_ids=["0", "1"],
        started_at=NOW,
    )
    with pytest.raises(ValidationError, match="not relay"):
        CameraRelayStarted.model_validate({**started.model_dump(), "source": CameraProvider.RELAY})
    stopped = CameraRelayStopped(
        relay_session_id=session_id,
        account_id=account,
        mower_id=mower,
        reason=RelayStopReason.IDLE,
        started_at=NOW,
        stopped_at=EXPIRES,
    )
    assert stopped.vision_seconds == 0
    assert (stopped.trigger, stopped.peak_viewers) == (None, 0)  # optional, older producers
    assert (started.trigger, started.started_by) == (None, None)
    with pytest.raises(ValidationError):
        CameraRelayStopped.model_validate({**stopped.model_dump(), "vision_seconds": -1})
    with pytest.raises(ValidationError):
        CameraRelayStopped.model_validate({**stopped.model_dump(), "peak_viewers": -1})
    rich = CameraRelayStopped.model_validate(
        {**stopped.model_dump(), "trigger": "viewer", "peak_viewers": 3}
    )
    assert (rich.trigger, rich.peak_viewers) == (RelayPurpose.VIEWER, 3)
    opened = CameraRelayLeaseOpened(
        lease_id=lease,
        relay_session_id=session_id,
        account_id=account,
        mower_id=mower,
        purpose=RelayPurpose.VIEWER,
        requested_by=REF,
        camera_ids=["0"],
        opened_at=NOW,
    )
    closed = CameraRelayLeaseClosed(
        lease_id=lease,
        relay_session_id=session_id,
        account_id=account,
        mower_id=mower,
        purpose=RelayPurpose.VIEWER,
        reason=RelayLeaseCloseReason.DETACHED,
        opened_at=NOW,
        closed_at=EXPIRES,
    )
    payloads = (started, stopped, opened, closed)
    for event_type, payload in zip(CAMERA_RELAY_EVENT_TYPES, payloads, strict=True):
        envelope = EventEnvelope[dict[str, Any]](
            type=event_type,
            account_id=account,
            producer="camera-relay",
            payload=payload.model_dump(mode="json"),
        )
        again = EventEnvelope[dict[str, Any]].model_validate_json(envelope.model_dump_json())
        assert type(payload).model_validate(again.payload) == payload
        kind = event_type.rsplit(".", 1)[1]
        assert kind in CAMERA_RELAY_EVENT_KINDS
        subject = camera_relay_event_subject(mower, kind)  # type: ignore[arg-type]
        assert parse_camera_relay_event_subject(subject) == (mower, kind)


def test_relay_event_subjects_live_on_the_camera_stream() -> None:
    mower = uuid4()
    for kind in CAMERA_RELAY_EVENT_KINDS:
        subject = camera_relay_event_subject(mower, kind)  # type: ignore[arg-type]
        assert subject == f"camera.mower.{mower}.relay.{kind}"
        assert stream_for_subject(subject) is CAMERA
        assert subject_matches(ALL_CAMERA_RELAY_EVENTS, subject)
    assert stream_for_subject(ALL_CAMERA_RELAY_EVENTS) is CAMERA
    with pytest.raises(ValueError, match="invalid camera relay event kind"):
        camera_relay_event_subject(mower, "paused")  # type: ignore[arg-type]
    for bad in (
        f"camera.mower.{mower}.relay.paused",
        f"camera.mower.{mower}.other.started",
        f"device.mower.{mower}.relay.started",
        "camera.mower.relay.started",
    ):
        with pytest.raises(ValueError, match="camera relay event"):
            parse_camera_relay_event_subject(bad)
    with pytest.raises(ValueError):
        parse_camera_relay_event_subject("camera.mower.not-a-uuid.relay.started")


def test_relay_requests_are_outside_every_stream() -> None:
    assert RELAY_CAMERA_REQUESTS == (RELAY_CAMERA_ATTACH, RELAY_CAMERA_RENEW, RELAY_CAMERA_DETACH)
    for subject in RELAY_CAMERA_REQUESTS:
        assert stream_for_subject(subject) is None
