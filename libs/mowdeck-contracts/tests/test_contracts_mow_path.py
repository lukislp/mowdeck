"""The mow path: the contract, its snapshot, the preview request and reply, the subjects."""

from datetime import UTC, datetime
from uuid import UUID

import pytest
from mowdeck_contracts.v1 import (
    MAX_PATH_POINTS,
    MowerPathSnapshot,
    MowPath,
    MowPathProgress,
    PathPreviewError,
    PathPreviewErrorCode,
    PathPreviewGatewayRequest,
    PathPreviewReply,
    PathPreviewRequest,
    PrincipalKind,
    PrincipalRef,
)
from mowdeck_contracts.v1.subjects import (
    ALL_DEVICE_SNAPSHOTS,
    ALL_PATH_PREVIEWS,
    DEVICE,
    device_subject,
    parse_device_subject,
    parse_path_preview_subject,
    path_preview_subject,
    stream_for_subject,
    subject_matches,
)
from pydantic import ValidationError

MOWER = UUID("6f1c2b8e-3d4a-4f5b-8c9d-0e1f2a3b4c5d")
ACCOUNT = UUID("0b8f7a52-1111-4a0e-9a59-2f3c4d5e6f70")
AT = datetime(2026, 10, 11, 9, tzinfo=UTC)


def _path(**changes: object) -> MowPath:
    data: dict[str, object] = {
        "path_hash": "18446744073709551557",
        "polylines": [[(0.0, 0.0), (4.0, 0.0)], [(4.0, 0.5), (0.0, 0.5), (0.0, 1.0)]],
        "kinds": ["edge", "mow"],
        "points_total": 5,
        "planned_at": AT,
    }
    data.update(changes)
    return MowPath.model_validate(data)


def test_path_round_trips_and_keeps_the_hash_as_text() -> None:
    path = _path(progress=MowPathProgress(index=3, direction=1), area_ids=["1"])
    again = MowPath.model_validate_json(path.model_dump_json())
    assert again == path
    assert '"path_hash":"18446744073709551557"' in path.model_dump_json()


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"points_total": 4}, "points_total"),
        ({"kinds": ["mow"]}, "kinds"),
        ({"progress": {"index": 6}}, "beyond"),
        ({"polylines": [[]], "points_total": 0, "kinds": None}, "at least 1"),
        ({"path_hash": ""}, "at least 1"),
    ],
)
def test_path_rejects_inconsistent_data(changes: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _path(**changes)


def test_path_has_a_point_limit() -> None:
    line = [(float(i), 0.0) for i in range(MAX_PATH_POINTS + 1)]
    with pytest.raises(ValidationError, match="at most"):
        _path(polylines=[line], points_total=len(line), kinds=None)


def test_empty_path_and_kinds_are_optional() -> None:
    path = MowPath(path_hash="7", points_total=0, planned_at=AT)
    assert path.polylines == []
    assert path.kinds is None
    assert path.progress is None


def test_snapshot_without_path_means_no_job() -> None:
    cleared = MowerPathSnapshot(mower_id=MOWER, account_id=ACCOUNT, observed_at=AT)
    assert cleared.path is None
    full = MowerPathSnapshot(mower_id=MOWER, account_id=ACCOUNT, observed_at=AT, path=_path())
    assert MowerPathSnapshot.model_validate_json(full.model_dump_json()) == full


def test_path_is_a_device_snapshot_kind() -> None:
    subject = device_subject(MOWER, "path")
    assert subject == f"device.mower.{MOWER}.path"
    assert parse_device_subject(subject) == (MOWER, "path")
    assert stream_for_subject(subject) is DEVICE
    assert subject_matches(ALL_DEVICE_SNAPSHOTS, subject)


def test_preview_subject_round_trip_and_not_on_a_stream() -> None:
    subject = path_preview_subject(MOWER)
    assert subject == f"gateway.mower.{MOWER}.path.preview"
    assert parse_path_preview_subject(subject) == MOWER
    assert subject_matches(ALL_PATH_PREVIEWS, subject)
    assert stream_for_subject(subject) is None
    for other in (f"gateway.mower.{MOWER}.camera.session", f"device.mower.{MOWER}.path"):
        with pytest.raises(ValueError, match="path preview"):
            parse_path_preview_subject(other)


def test_preview_request_needs_areas() -> None:
    assert PathPreviewRequest(area_ids=["1"]).pattern is None
    with pytest.raises(ValidationError):
        PathPreviewRequest(area_ids=[])
    request = PathPreviewGatewayRequest(
        mower_id=MOWER,
        account_id=ACCOUNT,
        area_ids=["1", "2"],
        pattern={"name": "stripes"},
        requested_by=PrincipalRef(subject=ACCOUNT, kind=PrincipalKind.USER),
    )
    assert PathPreviewGatewayRequest.model_validate_json(request.model_dump_json()) == request


def test_preview_reply_holds_exactly_one_of_path_or_error() -> None:
    assert PathPreviewReply(path=_path()).error is None
    error = PathPreviewError(code=PathPreviewErrorCode.TIMEOUT, detail="late")
    assert PathPreviewReply(error=error).path is None
    with pytest.raises(ValidationError, match="exactly one"):
        PathPreviewReply()
    with pytest.raises(ValidationError, match="exactly one"):
        PathPreviewReply(path=_path(), error=error)
