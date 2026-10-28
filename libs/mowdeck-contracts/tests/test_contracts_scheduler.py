"""Contracts of the Scheduler and the internal Fleet requests it uses."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import (
    SCHEDULE_PATTERN_KEYS,
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
    MowerCapabilities,
    ObstacleDetection,
    PathOrder,
    PrincipalKind,
    PrincipalRef,
    ResolveTargetReply,
    ResolveTargetRequest,
    SchedulePattern,
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
    assert run.angle_deg is None


def test_contracts_run_event_carries_the_angle_of_a_started_run() -> None:
    base = {
        "run_id": str(uuid4()),
        "schedule_id": str(uuid4()),
        "account_id": str(uuid4()),
        "trigger": "schedule",
        "outcome": "started",
        "scheduled_for": NOW.isoformat(),
        "started_at": NOW.isoformat(),
    }
    run = ScheduleRunRecorded.model_validate({**base, "angle_deg": 135})
    assert run.angle_deg == 135.0
    assert ScheduleRunRecorded.model_validate_json(run.model_dump_json()) == run
    # An event of an older Scheduler has no angle: still valid (tolerant reader).
    assert ScheduleRunRecorded.model_validate(base).angle_deg is None
    for angle in (-1, 180, 270):
        with pytest.raises(ValidationError):
            ScheduleRunRecorded.model_validate({**base, "angle_deg": angle})


def test_contracts_scheduler_subjects_and_command_view_origin() -> None:
    assert FLEET_COMMANDS_ISSUE == "fleet.commands.issue"
    assert stream_for_subject(FLEET_COMMANDS_ISSUE) is None  # core NATS, never a stream
    assert stream_for_subject(FLEET_TARGETS_RESOLVE) is None
    assert event_subject_for_type(SCHEDULER_RUN_RECORDED) == "event.scheduler.run.recorded"
    assert event_subject_for_type(SCHEDULER_SCHEDULE_DISABLED) == (
        "event.scheduler.schedule.disabled"
    )
    assert "origin" in CommandView.model_fields and "schedule_id" in CommandView.model_fields


# --- SchedulePattern (ADR 0024 amendment 2026-10-24) -------------------------------------------


def _caps(**values: object) -> MowerCapabilities:
    return MowerCapabilities.model_validate(values)


AWD = {
    "native_patterns": ["stripes", "diagonal", "checkerboard"],
    "path_spacing_range_mm": [200, 350],
    "start_options": {
        "obstacle_detection": ["off", "standard", "sensitive"],
        "obstacle_laps_range": [0, 4],
        "orders": ["border_first", "grid_first"],
        "start_progress_range_pct": [0, 99],
    },
}


def test_schedule_pattern_is_all_optional_and_dumps_only_what_is_set() -> None:
    assert SchedulePattern().keys() == {}
    pattern = SchedulePattern(
        name="diagonal", angle_deg=45, obstacle_detection=ObstacleDetection.SENSITIVE
    )
    assert pattern.keys() == {
        "name": "diagonal",
        "angle_deg": 45.0,
        "obstacle_detection": "sensitive",
    }
    assert set(SchedulePattern.model_fields) == set(SCHEDULE_PATTERN_KEYS)


def test_schedule_pattern_has_no_start_progress() -> None:
    # A tolerant reader ignores the key; the Scheduler API refuses it (extra fields are 422).
    assert "start_progress_pct" not in SchedulePattern.model_fields
    assert SchedulePattern.model_validate({"start_progress_pct": 40}).keys() == {}


@pytest.mark.parametrize(
    "values",
    [
        {"name": "spiral"},
        {"angle_deg": -1},
        {"angle_deg": 181},
        {"border_laps": 5},
        {"spacing_m": 0.01},
        {"obstacle_detection": "loud"},
        {"obstacle_laps": 5},
        {"order": "sideways"},
    ],
)
def test_schedule_pattern_refuses_values_out_of_the_contract(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SchedulePattern.model_validate(values)


def test_schedule_pattern_problem_follows_the_capabilities() -> None:
    awd = _caps(**AWD)
    bare = _caps(native_patterns=["stripes"])
    full = SchedulePattern(
        name="diagonal",
        angle_deg=30,
        border_laps=2,
        spacing_m=0.3,
        obstacle_detection=ObstacleDetection.SENSITIVE,
        obstacle_laps=1,
        order=PathOrder.GRID_FIRST,
    )
    assert full.problem(awd) is None
    # Angle and border laps need no declaration.
    assert SchedulePattern(angle_deg=90, border_laps=4).problem(bare) is None
    assert SchedulePattern(name="diagonal").problem(bare) == "pattern 'diagonal' is not offered"
    assert "spacing" in (SchedulePattern(spacing_m=0.3).problem(bare) or "")
    assert "between 0.2 and 0.35" in (SchedulePattern(spacing_m=0.4).problem(awd) or "")
    assert "obstacle detection" in (
        SchedulePattern(obstacle_detection=ObstacleDetection.STANDARD).problem(bare) or ""
    )
    assert "no-go" in (SchedulePattern(obstacle_laps=1).problem(bare) or "")
    assert "path order" in (SchedulePattern(order=PathOrder.GRID_FIRST).problem(bare) or "")
