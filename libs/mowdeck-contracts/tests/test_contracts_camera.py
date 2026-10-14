"""Camera session contracts (ADR 0032)."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import (
    CameraEncryption,
    CameraInfo,
    CameraPlacement,
    CameraProvider,
    CameraQuota,
    CameraSession,
    CameraSessionError,
    CameraSessionErrorCode,
    CameraSessionReply,
    CameraSessionRequest,
    PrincipalKind,
    PrincipalRef,
)
from mowdeck_contracts.v1.camera import REDACTED
from mowdeck_contracts.v1.subjects import (
    ALL_CAMERA_SESSIONS,
    camera_session_subject,
    parse_camera_session_subject,
    stream_for_subject,
    subject_matches,
)
from pydantic import ValidationError

SECRET = "007secret-token"  # noqa: S105 - a test value
EXPIRES = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


def _session(**changes: object) -> CameraSession:
    values: dict[str, object] = {
        "provider": CameraProvider.AGORA,
        "app_id": "app",
        "channel": "chan",
        "token": SECRET,
        "uid": 1234,
        "cameras": [CameraInfo(id="0", name="Front left", uid=1)],
        "encryption": CameraEncryption(mode="aes-256-gcm2", key="k" * 32, salt="c2FsdA=="),
        "expires_at": EXPIRES,
        "quota": CameraQuota(used_minutes=12, total_minutes=600),
    }
    values.update(changes)
    return CameraSession.model_validate(values)


def test_camera_session_hides_secrets_in_repr_and_redacted_dump() -> None:
    session = _session()
    text = repr(session)
    assert SECRET not in text
    assert "kkkk" not in text
    assert "c2FsdA==" not in text
    dump = session.redacted()
    assert dump["token"] == REDACTED
    assert dump["encryption"]["key"] == "<redacted>"
    assert dump["encryption"]["salt"] == "<redacted>"
    assert dump["channel"] == "chan"
    assert dump["cameras"] == [{"id": "0", "name": "Front left", "uid": 1, "placement": None}]


def test_camera_info_placement_is_optional_and_validated() -> None:
    left = CameraInfo(id="0", name="Front left", uid=1, placement=CameraPlacement.FRONT_LEFT)
    assert left.model_dump(mode="json")["placement"] == "front_left"
    assert CameraInfo.model_validate({"id": "x", "name": "X"}).placement is None
    assert CameraInfo.model_validate(
        {"id": "1", "name": "R", "placement": "front_right"}
    ).placement is (CameraPlacement.FRONT_RIGHT)
    with pytest.raises(ValidationError):
        CameraInfo.model_validate({"id": "1", "name": "R", "placement": "roof"})


def test_camera_session_round_trips_with_secrets_in_json() -> None:
    session = _session(encryption=None, quota=None)
    again = CameraSession.model_validate_json(session.model_dump_json())
    assert again == session
    assert again.token == SECRET
    assert session.redacted()["encryption"] is None


def test_camera_session_rejects_naive_expiry_and_bad_uid() -> None:
    with pytest.raises(ValidationError):
        _session(expires_at=datetime(2026, 10, 10, 12, 0))
    with pytest.raises(ValidationError):
        _session(uid=-1)


def test_camera_session_reply_needs_exactly_one_part() -> None:
    error = CameraSessionError(code=CameraSessionErrorCode.UNSUPPORTED, detail="no camera")
    assert CameraSessionReply(error=error).session is None
    assert CameraSessionReply(session=_session()).error is None
    with pytest.raises(ValidationError):
        CameraSessionReply()
    with pytest.raises(ValidationError):
        CameraSessionReply(session=_session(), error=error)


def test_camera_session_request_carries_the_principal() -> None:
    ref = PrincipalRef(subject=uuid4(), kind=PrincipalKind.USER)
    request = CameraSessionRequest(mower_id=uuid4(), account_id=uuid4(), requested_by=ref)
    assert CameraSessionRequest.model_validate_json(request.model_dump_json()) == request


def test_camera_session_subject_is_outside_every_stream() -> None:
    mower_id = uuid4()
    subject = camera_session_subject(mower_id)
    assert subject == f"gateway.mower.{mower_id}.camera.session"
    assert stream_for_subject(subject) is None
    assert stream_for_subject(ALL_CAMERA_SESSIONS) is None
    assert subject_matches(ALL_CAMERA_SESSIONS, subject)
    assert parse_camera_session_subject(subject) == mower_id
    with pytest.raises(ValueError, match="not a camera session subject"):
        parse_camera_session_subject(f"device.mower.{mower_id}.map")
