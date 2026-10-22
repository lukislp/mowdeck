"""Vision contracts: photo request and reply, the proposal event, drive origin, new fields."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import (
    CAPTURE_MAX_BYTES,
    FEATURE_AI_RECOVER,
    FEATURE_AI_RECOVER_HOSTED,
    PHOTO_MAX_BYTES,
    VISION_PROPOSAL_CREATED,
    CameraProvider,
    CameraSession,
    CameraSnapshotRequest,
    ConnectionKind,
    DriveEndReason,
    DriveSessionView,
    DriveStartRequest,
    IssueCommandRequest,
    MowerActivity,
    MowerState,
    NotificationKind,
    PhotoError,
    PhotoErrorCode,
    PhotoReply,
    PhotoRequest,
    PrincipalKind,
    PrincipalRef,
    ReturnToDock,
    VisionProposalCreated,
    VisionTask,
    VisionTrigger,
)
from mowdeck_contracts.v1.commands import CommandRequest
from mowdeck_contracts.v1.subjects import (
    ALL_PHOTO_REQUESTS,
    event_subject_for_type,
    parse_photo_subject,
    photo_subject,
    stream_for_subject,
    subject_matches,
)
from pydantic import ValidationError

T0 = datetime(2026, 10, 10, 8, 0, tzinfo=UTC)


def _ref() -> PrincipalRef:
    return PrincipalRef(subject=uuid4(), kind=PrincipalKind.USER)


def test_photo_subject_is_core_nats_and_round_trips() -> None:
    mower_id = uuid4()
    subject = photo_subject(mower_id)
    assert subject == f"gateway.mower.{mower_id}.photo"
    assert stream_for_subject(subject) is None, "request/reply must not be captured by a stream"
    assert subject_matches(ALL_PHOTO_REQUESTS, subject)
    assert parse_photo_subject(subject) == mower_id


@pytest.mark.parametrize(
    "subject", ["gateway.mower.x", "device.mower.1.photo", "gateway.mower.abc.map", "a.b.c.d"]
)
def test_parse_photo_subject_rejects_other_subjects(subject: str) -> None:
    with pytest.raises(ValueError, match="photo"):
        parse_photo_subject(subject)


def test_photo_request_caps_the_size() -> None:
    request = PhotoRequest(mower_id=uuid4(), account_id=uuid4())
    assert request.max_bytes == PHOTO_MAX_BYTES
    with pytest.raises(ValidationError):
        PhotoRequest(mower_id=uuid4(), account_id=uuid4(), max_bytes=PHOTO_MAX_BYTES + 1)


def test_photo_reply_has_exactly_an_image_or_an_error() -> None:
    ok = PhotoReply(image_base64="aGk=", media_type="image/png", taken_at=T0)
    assert PhotoReply.model_validate_json(ok.model_dump_json()) == ok
    failed = PhotoReply(error=PhotoError(code=PhotoErrorCode.UNSUPPORTED, detail="no camera"))
    assert failed.image_base64 is None
    with pytest.raises(ValidationError):
        PhotoReply()
    with pytest.raises(ValidationError):
        PhotoReply(image_base64="aGk=")  # media type missing
    with pytest.raises(ValidationError):
        PhotoReply(
            image_base64="aGk=",
            media_type="image/png",
            error=PhotoError(code=PhotoErrorCode.FAILED, detail="x"),
        )


def test_vision_proposal_event_round_trips() -> None:
    event = VisionProposalCreated(
        proposal_id=uuid4(),
        mower_id=uuid4(),
        account_id=uuid4(),
        task=VisionTask.RECOVER,
        round=1,
        requested_by=_ref(),
        situation="stuck_on_obstacle",
        confidence=0.8,
        steps=3,
        expires_at=T0 + timedelta(minutes=5),
        link="/mowers/x?assist=y",
        created_at=T0,
    )
    assert VisionProposalCreated.model_validate_json(event.model_dump_json()) == event
    assert event_subject_for_type(VISION_PROPOSAL_CREATED) == "event.vision.proposal.created"
    with pytest.raises(ValidationError):
        VisionProposalCreated.model_validate({**event.model_dump(), "steps": 7})


def test_feature_keys_are_siblings_not_children() -> None:
    assert FEATURE_AI_RECOVER == "ai_recover"
    assert not FEATURE_AI_RECOVER_HOSTED.startswith(f"{FEATURE_AI_RECOVER}.")


def test_drive_origin_and_new_end_reasons() -> None:
    request = DriveStartRequest(
        on_behalf_of=_ref(), account_id=uuid4(), mower_id=uuid4(), max_seconds=30, origin="vision"
    )
    assert request.origin == "vision"
    assert DriveStartRequest.model_validate_json(request.model_dump_json()) == request
    with pytest.raises(ValidationError):
        DriveStartRequest(
            on_behalf_of=_ref(),
            account_id=uuid4(),
            mower_id=uuid4(),
            max_seconds=30,
            origin="x",  # type: ignore[arg-type]
        )
    assert {DriveEndReason.COMPLETED, DriveEndReason.ABORTED} <= set(DriveEndReason)
    view = DriveSessionView(
        session_id=uuid4(),
        mower_id=uuid4(),
        started_by=_ref(),
        started_at=T0,
        expires_at=T0,
        blade_on_allowed=False,
        origin="vision",
    )
    assert view.origin == "vision"


def test_vision_may_issue_commands_on_behalf_of_a_subject() -> None:
    request = IssueCommandRequest(
        on_behalf_of=_ref(),
        account_id=uuid4(),
        mower_id=uuid4(),
        command=CommandRequest(command=ReturnToDock()),
        origin="vision",
    )
    assert request.origin == "vision"


def test_mower_state_drive_recoverable_defaults_to_false() -> None:
    state = MowerState(
        mower_id=uuid4(),
        account_id=uuid4(),
        observed_at=T0,
        activity=MowerActivity.ERROR,
        battery_percent=50,
        connection=ConnectionKind.SIMULATED,
        error_code="stuck",
    )
    assert state.drive_recoverable is False
    assert state.model_copy(update={"drive_recoverable": True}).drive_recoverable


def test_vision_proposal_notification_kind_exists() -> None:
    assert NotificationKind("vision_proposal") is NotificationKind.VISION_PROPOSAL


def test_vision_proposal_event_trigger_is_manual_unless_an_error_started_it() -> None:
    values = {
        "proposal_id": uuid4(),
        "mower_id": uuid4(),
        "account_id": uuid4(),
        "task": "recover",
        "round": 1,
        "requested_by": _ref().model_dump(mode="json"),
        "situation": "stuck_on_obstacle",
        "confidence": 0.8,
        "steps": 2,
        "expires_at": (T0 + timedelta(minutes=5)).isoformat(),
        "link": "/mowers/x?assist=y",
        "created_at": T0.isoformat(),
    }
    older = VisionProposalCreated.model_validate(values)  # a producer without the new fields
    assert older.trigger is VisionTrigger.MANUAL and older.error_code is None
    automatic = VisionProposalCreated.model_validate(
        {**values, "trigger": "error", "error_code": "1201"}
    )
    assert automatic.trigger is VisionTrigger.ERROR and automatic.error_code == "1201"
    assert NotificationKind("vision_auto_proposal") is NotificationKind.VISION_AUTO_PROPOSAL
    with pytest.raises(ValidationError):
        VisionProposalCreated.model_validate({**values, "error_code": "x" * 65})


def test_camera_snapshot_request_bounds_and_keeps_the_token_out_of_repr() -> None:
    session = CameraSession(
        provider=CameraProvider.AGORA,
        app_id="app",
        channel="chan",
        token="secret-agora-token",  # noqa: S106 - a test value
        uid=7,
        expires_at=T0,
    )
    request = CameraSnapshotRequest(session=session, camera_uid=1)
    assert request.timeout_seconds == 20.0 and request.max_bytes == CAPTURE_MAX_BYTES
    assert "secret-agora-token" not in repr(request)
    assert CameraSnapshotRequest.model_validate_json(request.model_dump_json()) == request
    for bad in (
        {"timeout_seconds": 0},
        {"timeout_seconds": 61},
        {"max_bytes": 10},
        {"camera_uid": -1},
    ):
        with pytest.raises(ValidationError):
            CameraSnapshotRequest.model_validate({"session": session, **bad})


def test_vision_proposal_updated_round_trip() -> None:
    from datetime import UTC, datetime
    from uuid import uuid4

    from mowdeck_contracts.v1 import (
        VISION_FINAL_STATES,
        VISION_PROPOSAL_UPDATED,
        VisionProposalState,
        VisionProposalUpdated,
        VisionTask,
    )

    event = VisionProposalUpdated(
        proposal_id=uuid4(),
        mower_id=uuid4(),
        account_id=uuid4(),
        task=VisionTask.RECOVER,
        round=1,
        state=VisionProposalState.EXECUTING,
        final=False,
        action="drive",
        steps=2,
        link="/mowers/x?assist=y",
        updated_at=datetime(2026, 10, 22, 12, 0, tzinfo=UTC),
    )
    assert VisionProposalUpdated.model_validate_json(event.model_dump_json()) == event
    assert event.expires_at is None and event.trigger == "manual"
    assert VISION_PROPOSAL_UPDATED == "vision.proposal.updated"
    assert VisionProposalState.PROPOSED not in VISION_FINAL_STATES
    assert VisionProposalState.RECOVERED in VISION_FINAL_STATES
