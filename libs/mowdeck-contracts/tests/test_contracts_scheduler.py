"""Contracts of the Scheduler and the internal Fleet requests it uses."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import (
    SCHEDULER_RUN_RECORDED,
    SCHEDULER_SCHEDULE_DISABLED,
    CommandAccepted,
    CommandPriority,
    CommandRequest,
    CommandView,
    IssueCommandError,
    IssueCommandErrorCode,
    IssueCommandReply,
    IssueCommandRequest,
    PrincipalKind,
    PrincipalRef,
    ResolveTargetReply,
    ResolveTargetRequest,
    ScheduleRunOutcome,
    ScheduleRunRecorded,
    StartMow,
    TargetMower,
)
from mowdeck_contracts.v1.subjects import (
    FLEET_COMMANDS_ISSUE,
    FLEET_TARGETS_RESOLVE,
    event_subject_for_type,
    stream_for_subject,
)
from pydantic import ValidationError

NOW = datetime(2026, 10, 9, 8, 0, tzinfo=UTC)


def _accepted() -> CommandAccepted:
    return CommandAccepted(
        command_id=uuid4(),
        mower_id=uuid4(),
        kind="start_mow",
        priority=CommandPriority.NORMAL,
        issued_at=NOW,
        expires_at=NOW,
        correlation_id="c-1",
        status_url="/api/v1/fleet/commands/x",
    )


def test_contracts_issue_request_defaults_and_round_trip() -> None:
    request = IssueCommandRequest(
        on_behalf_of=PrincipalRef(subject=uuid4(), kind=PrincipalKind.USER),
        account_id=uuid4(),
        mower_id=uuid4(),
        command=CommandRequest(command=StartMow(area_ids=["front"], pattern={"angle": 45})),
        schedule_id=uuid4(),
    )
    assert request.origin == "scheduler"
    assert IssueCommandRequest.model_validate_json(request.model_dump_json()) == request
    with pytest.raises(ValidationError):
        IssueCommandRequest.model_validate({**request.model_dump(), "origin": "someone"})


def test_contracts_issue_reply_has_exactly_one_answer() -> None:
    assert IssueCommandReply(accepted=_accepted()).error is None
    refused = IssueCommandReply(
        error=IssueCommandError(code=IssueCommandErrorCode.FORBIDDEN, detail="missing can_control")
    )
    assert refused.accepted is None
    with pytest.raises(ValidationError):
        IssueCommandReply()
    with pytest.raises(ValidationError):
        IssueCommandReply(accepted=_accepted(), error=refused.error)


def test_contracts_resolve_target_and_run_event() -> None:
    request = ResolveTargetRequest(account_id=uuid4(), target_type="group", target_id=uuid4())
    assert request.target_type == "group"
    with pytest.raises(ValidationError):
        ResolveTargetRequest(account_id=uuid4(), target_type="site", target_id=uuid4())  # type: ignore[arg-type]
    reply = ResolveTargetReply(
        found=True,
        timezone="Europe/Berlin",
        mowers=[
            TargetMower(
                mower_id=uuid4(), name="A", site_id=uuid4(), timezone="Europe/Berlin", latitude=52
            )
        ],
    )
    assert reply.mowers[0].longitude is None
    with pytest.raises(ValidationError):
        TargetMower(mower_id=uuid4(), name="A", site_id=uuid4(), timezone="X", latitude=91)
    run = ScheduleRunRecorded(
        run_id=uuid4(),
        schedule_id=uuid4(),
        account_id=uuid4(),
        trigger="schedule",
        outcome=ScheduleRunOutcome.SKIPPED_RAIN,
        scheduled_for=NOW,
        started_at=NOW,
    )
    assert run.mower_id is None and run.outcome == "skipped_rain"


def test_contracts_scheduler_subjects_and_command_view_origin() -> None:
    assert FLEET_COMMANDS_ISSUE == "fleet.commands.issue"
    assert stream_for_subject(FLEET_COMMANDS_ISSUE) is None  # core NATS, never a stream
    assert stream_for_subject(FLEET_TARGETS_RESOLVE) is None
    assert event_subject_for_type(SCHEDULER_RUN_RECORDED) == "event.scheduler.run.recorded"
    assert event_subject_for_type(SCHEDULER_SCHEDULE_DISABLED) == (
        "event.scheduler.schedule.disabled"
    )
    assert "origin" in CommandView.model_fields and "schedule_id" in CommandView.model_fields
