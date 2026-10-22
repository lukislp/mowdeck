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


def test_notification_raised_round_trip_and_defaults() -> None:
    from datetime import UTC, datetime
    from uuid import uuid4

    import pytest
    from mowdeck_contracts.v1 import (
        NOTIFICATION_RAISED,
        NOTIFICATIONS_VIEW_PERMISSION,
        NotificationKind,
        NotificationRaised,
        NotificationSent,
        NotificationSeverity,
    )
    from pydantic import ValidationError

    now = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
    raised = NotificationRaised(
        notification_id=uuid4(),
        account_id=uuid4(),
        mower_id=uuid4(),
        kind=NotificationKind.MOWER_LIFTED,
        severity=NotificationSeverity.CRITICAL,
        title="Backyard was lifted",
        body="The lift sensor stopped the mower.",
        occurred_at=now,
        created_at=now,
        dedupe_key="mower_lifted:x:1",
    )
    assert raised.required_relation == "can_view"
    assert NotificationRaised.model_validate_json(raised.model_dump_json()) == raised
    assert NOTIFICATION_RAISED == "notifications.raised"
    assert NOTIFICATIONS_VIEW_PERMISSION == "notifications.view"
    with pytest.raises(ValidationError):
        NotificationRaised.model_validate(
            {**raised.model_dump(), "required_relation": "can_control"}
        )
    # notifications.sent from before created_at still validates.
    old = NotificationSent(
        notification_id=uuid4(),
        user_id=uuid4(),
        account_id=uuid4(),
        kind=NotificationKind.TEST,
        severity=NotificationSeverity.INFO,
        title="t",
        body="b",
        occurred_at=now,
    )
    assert old.created_at is None
