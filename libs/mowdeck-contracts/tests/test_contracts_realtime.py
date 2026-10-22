"""The WebSocket protocol of Realtime: parsing client messages, round trips of server messages."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import (
    CommandResult,
    CommandStatus,
    ConnectionKind,
    MowerActivity,
    MowerState,
    PrincipalKind,
)
from mowdeck_contracts.v1.realtime import (
    AuthMessage,
    CommandResultMessage,
    ErrorMessage,
    PingMessage,
    PongMessage,
    PrincipalSummary,
    ReadyMessage,
    RealtimeClientMessage,
    RealtimeCloseCode,
    RealtimeErrorCode,
    RealtimeServerMessage,
    RevokedMessage,
    RevokeReason,
    StateMessage,
    SubscribedMessage,
    SubscribeMessage,
    UnsubscribedMessage,
    UnsubscribeMessage,
)
from pydantic import ValidationError

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def test_realtime_client_messages_are_discriminated_by_type() -> None:
    mower = uuid4()
    cases = {
        '{"type": "auth", "token": "abc"}': AuthMessage,
        f'{{"type": "subscribe", "request_id": "r1", "mower_ids": ["{mower}"]}}': SubscribeMessage,
        f'{{"type": "unsubscribe", "mower_ids": ["{mower}"]}}': UnsubscribeMessage,
        '{"type": "ping"}': PingMessage,
    }
    for raw, model in cases.items():
        assert isinstance(RealtimeClientMessage.model_validate_json(raw).root, model)
    subscribe = RealtimeClientMessage.model_validate_json(
        f'{{"type": "subscribe", "mower_ids": ["{mower}"], "extra": 1}}'
    ).root
    assert isinstance(subscribe, SubscribeMessage)
    assert subscribe.mower_ids == [mower]


@pytest.mark.parametrize(
    "raw",
    [
        '{"type": "fly"}',
        '{"token": "abc"}',
        '{"type": "auth", "token": ""}',
        '{"type": "subscribe", "mower_ids": []}',
        '{"type": "subscribe", "mower_ids": ["not-a-uuid"]}',
        '{"type": "ping", "request_id": "has space"}',
        "not json",
    ],
)
def test_realtime_invalid_client_messages_are_rejected(raw: str) -> None:
    with pytest.raises(ValidationError):
        RealtimeClientMessage.model_validate_json(raw)


def test_realtime_server_messages_round_trip() -> None:
    mower = uuid4()
    state = MowerState(
        mower_id=mower,
        account_id=uuid4(),
        observed_at=NOW,
        activity=MowerActivity.MOWING,
        battery_percent=80,
        connection=ConnectionKind.SIMULATED,
    )
    messages = [
        ReadyMessage(
            connection_id=uuid4(),
            principal=PrincipalSummary(
                subject=uuid4(), account_id=uuid4(), kind=PrincipalKind.SUBUSER
            ),
            token_expires_at=NOW,
            server_time=NOW,
        ),
        SubscribedMessage(request_id="r1", mower_ids=[mower], denied=[uuid4()]),
        UnsubscribedMessage(mower_ids=[mower]),
        StateMessage(mower_id=mower, state=state),
        CommandResultMessage(
            mower_id=mower,
            result=CommandResult(command_id=uuid4(), mower_id=mower, status=CommandStatus.EXECUTED),
        ),
        RevokedMessage(mower_ids=[mower], reason=RevokeReason.ACCESS_REVOKED),
        ErrorMessage(code=RealtimeErrorCode.INVALID_MESSAGE, message="bad"),
        PongMessage(request_id="p"),
    ]
    for message in messages:
        parsed = RealtimeServerMessage.model_validate_json(message.model_dump_json()).root
        assert parsed == message


def test_realtime_close_codes_are_stable() -> None:
    assert int(RealtimeCloseCode.UNAUTHORIZED) == 4401
    assert int(RealtimeCloseCode.LIMIT_EXCEEDED) == 4429
    assert int(RealtimeCloseCode.SLOW_CONSUMER) == 4430
    assert int(RealtimeCloseCode.GOING_AWAY) == 1001


def test_realtime_notification_and_vision_proposal_messages_parse() -> None:
    from uuid import uuid4

    from mowdeck_contracts.v1.realtime import (
        NotificationMessage,
        RealtimeServerMessage,
        VisionProposalMessage,
    )

    mower_id = str(uuid4())
    note = RealtimeServerMessage.model_validate(
        {
            "type": "notification",
            "notification": {
                "id": str(uuid4()),
                "scope": "account",
                "mower_id": mower_id,
                "kind": "mower_lifted",
                "severity": "critical",
                "title": "t",
                "body": "b",
                "occurred_at": "2026-10-22T12:00:00Z",
                "created_at": "2026-10-22T12:00:01Z",
            },
        }
    ).root
    assert isinstance(note, NotificationMessage)
    assert note.notification.read is False and note.notification.acked_at is None
    proposal = RealtimeServerMessage.model_validate(
        {
            "type": "vision_proposal",
            "event": "updated",
            "mower_id": mower_id,
            "proposal": {
                "id": str(uuid4()),
                "mower_id": mower_id,
                "task": "find_dock",
                "round": 2,
                "state": "expired",
                "final": True,
                "link": "/mowers/x?assist=y",
                "updated_at": "2026-10-22T12:00:00Z",
            },
        }
    ).root
    assert isinstance(proposal, VisionProposalMessage)
    assert proposal.proposal.action is None and proposal.proposal.steps == 0
