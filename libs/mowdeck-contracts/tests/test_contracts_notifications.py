"""The ``notifications.sent`` event and the Web Push payload."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import (
    NOTIFICATION_SENT,
    EventEnvelope,
    NotificationKind,
    NotificationSent,
    NotificationSeverity,
    WebPushPayload,
)
from mowdeck_contracts.v1.subjects import event_subject_for_type, stream_for_subject
from pydantic import ValidationError


def test_notification_sent_round_trip_and_subject() -> None:
    sent = NotificationSent(
        notification_id=uuid4(),
        user_id=uuid4(),
        account_id=uuid4(),
        mower_id=uuid4(),
        kind=NotificationKind.MOWING_FINISHED,
        severity=NotificationSeverity.INFO,
        title="Front lawn finished mowing",
        body="Mowed for 42 min.",
        occurred_at=datetime(2026, 10, 9, 12, tzinfo=UTC),
        channels=["webpush"],
    )
    envelope = EventEnvelope[NotificationSent](
        type=NOTIFICATION_SENT, producer="notifications", account_id=sent.account_id, payload=sent
    )
    decoded = EventEnvelope[NotificationSent].model_validate_json(envelope.model_dump_json())
    assert decoded.payload == sent
    subject = event_subject_for_type(NOTIFICATION_SENT)
    assert subject == "event.notifications.sent"
    stream = stream_for_subject(subject)
    assert stream is not None and stream.name == "EVENTS"


def test_web_push_payload_validates() -> None:
    payload = WebPushPayload(title="t", body="b", tag="x", kind=NotificationKind.TEST)
    assert payload.url is None and payload.mower_id is None
    assert "kind" in payload.model_dump(mode="json")
    with pytest.raises(ValidationError):
        WebPushPayload(title="t" * 201, body="b", tag="x", kind=NotificationKind.TEST)
    with pytest.raises(ValidationError):
        WebPushPayload.model_validate({"title": "t", "body": "b", "tag": "x", "kind": "nope"})
