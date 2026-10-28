"""Theft tracking contracts (ADR 0058)."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import (
    NotificationKind,
    TrackingEvent,
    TrackingLeaseCode,
    TrackingLeaseReply,
    TrackingLeaseRequest,
)
from pydantic import ValidationError

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


def test_tracking_event_roundtrip_and_ranges() -> None:
    event = TrackingEvent(
        session_id=uuid4(),
        mower_id=uuid4(),
        account_id=uuid4(),
        at=NOW,
        trigger="geofence_exit",
        end_reason="max_duration",
        lat=48.1,
        lon=11.5,
        track_points=3,
    )
    assert TrackingEvent.model_validate_json(event.model_dump_json()) == event
    with pytest.raises(ValidationError):
        TrackingEvent(
            session_id=uuid4(),
            mower_id=uuid4(),
            account_id=uuid4(),
            at=NOW,
            trigger="manual",
            lat=91,
        )
    with pytest.raises(ValidationError):
        TrackingEvent(
            session_id=uuid4(),
            mower_id=uuid4(),
            account_id=uuid4(),
            at=NOW,
            trigger="burglar",  # type: ignore[arg-type]
        )


def test_lease_request_bounds_and_release() -> None:
    request = TrackingLeaseRequest(mower_id=uuid4(), account_id=uuid4())
    assert (request.interval_seconds, request.lease_seconds) == (10, 60)
    assert (
        TrackingLeaseRequest(mower_id=uuid4(), account_id=uuid4(), lease_seconds=0).lease_seconds
        == 0
    )
    for bad in ({"interval_seconds": 0.5}, {"lease_seconds": -1}, {"interval_seconds": 7200}):
        with pytest.raises(ValidationError):
            TrackingLeaseRequest(mower_id=uuid4(), account_id=uuid4(), **bad)


def test_lease_reply_and_notification_kinds() -> None:
    assert TrackingLeaseReply(code=TrackingLeaseCode.OK).interval_seconds is None
    assert {k.value for k in NotificationKind if k.value.startswith("tracking_")} == {
        "tracking_started",
        "tracking_moved",
        "tracking_stale",
        "tracking_ended",
    }
