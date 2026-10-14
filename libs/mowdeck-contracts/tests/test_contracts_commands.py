"""Fleet's command API contracts."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import (
    FINAL_COMMAND_STATES,
    BulkCommandAccepted,
    BulkCommandItem,
    BulkCommandOutcome,
    CommandAccepted,
    CommandPriority,
    CommandRequest,
    CommandState,
    CommandView,
    EmergencyStop,
    PrincipalKind,
    PrincipalRef,
    StartMow,
)
from pydantic import ValidationError

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def test_command_request_parses_the_command_union() -> None:
    request = CommandRequest.model_validate(
        {"command": {"kind": "start_mow", "area_ids": ["front"]}, "ttl_seconds": 60}
    )
    assert isinstance(request.command, StartMow)
    assert request.command.area_ids == ["front"]
    assert (
        CommandRequest.model_validate({"command": {"kind": "emergency_stop"}}).ttl_seconds is None
    )
    with pytest.raises(ValidationError):
        CommandRequest.model_validate({"command": {"kind": "fly"}})
    with pytest.raises(ValidationError):
        CommandRequest.model_validate({"command": {"kind": "pause_mow"}, "ttl_seconds": 0})


def test_accepted_and_view_round_trip() -> None:
    command_id, mower_id = uuid4(), uuid4()
    accepted = CommandAccepted(
        command_id=command_id,
        mower_id=mower_id,
        kind="emergency_stop",
        priority=CommandPriority.EMERGENCY,
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=10),
        correlation_id="c-1",
        status_url=f"/api/v1/fleet/commands/{command_id}",
    )
    assert accepted.status == "accepted"
    assert CommandAccepted.model_validate_json(accepted.model_dump_json()) == accepted
    view = CommandView(
        command_id=command_id,
        mower_id=mower_id,
        kind="start_mow",
        priority=CommandPriority.NORMAL,
        status=CommandState.EXECUTED,
        issued_by=PrincipalRef(subject=uuid4(), kind=PrincipalKind.USER),
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=30),
        result_at=NOW + timedelta(seconds=1),
        correlation_id="c-2",
    )
    assert CommandView.model_validate_json(view.model_dump_json()) == view
    assert view.status in FINAL_COMMAND_STATES
    assert CommandState.PENDING not in FINAL_COMMAND_STATES


def test_bulk_answer_lists_one_item_per_visible_mower() -> None:
    bulk = BulkCommandAccepted(
        bulk_id=uuid4(),
        target_type="group",
        target_id=uuid4(),
        kind=EmergencyStop().kind,
        items=[
            BulkCommandItem(
                mower_id=uuid4(), outcome=BulkCommandOutcome.ACCEPTED, command_id=uuid4()
            ),
            BulkCommandItem(mower_id=uuid4(), outcome=BulkCommandOutcome.FORBIDDEN),
        ],
    )
    again = BulkCommandAccepted.model_validate_json(bulk.model_dump_json())
    assert [item.outcome for item in again.items] == ["accepted", "forbidden"]
    with pytest.raises(ValidationError):
        BulkCommandAccepted.model_validate({**bulk.model_dump(), "target_type": "mower"})
