"""The job context and the ``resume`` rules (pure): which job the mower runs, which areas are
done, current and next, the entry of the way to the next area, when ``resume`` may be proposed
and when ``resume_mow`` may be sent; the prompt and schema that offer it."""

import math
from datetime import UTC, datetime
from uuid import uuid4

from mowdeck_contracts.v1 import (
    ConnectionKind,
    DockPose,
    GeoReference,
    MapArea,
    MapPath,
    MowerActivity,
    MowerMap,
    MowerState,
    MowPath,
    RtkFixStatus,
    SpotRunStatus,
    VisionTask,
)
from mowdeck_control.geo import to_geo_position

from vision.adapters.providers.prompt import ASSESSMENT_SCHEMA, PROMPT_MODE_SUFFIX, SYSTEM_PROMPT
from vision.domain.context import Motion, build_context
from vision.domain.dock import dock_route
from vision.domain.job import JobContext, job_context, resume_problem
from vision.domain.models import AssessmentOut, ProposalAction, ProposalState, ProposedStep
from vision.domain.rules import decide, evaluate, resume_gate

T0 = datetime(2026, 10, 22, 14, 30, tzinfo=UTC)
MOTION = Motion(linear_mps_at_1=0.5, turn_deg_per_s_at_1=57)

# The live case in small: "Vorderer Garten" with the dock, a channel (path) at its far end
# leading through a gate to "Hinterer Garten", the job mows the front first, then the back.
GARDEN = MowerMap(
    reference=GeoReference(origin_lat=49.7, origin_lon=11.7, rotation_deg=0.0),
    areas=[
        MapArea(id="11", name="Vorderer Garten", boundary=[(0, 0), (20, 0), (20, 10), (0, 10)]),
        MapArea(id="22", name="Hinterer Garten", boundary=[(0, 16), (20, 16), (20, 26), (0, 26)]),
        MapArea(id="33", name="Seite", boundary=[(30, 0), (40, 0), (40, 10), (30, 10)]),
    ],
    paths=[MapPath(id="p1", name="Durchgang", points=[(18, 10), (18, 16)])],
    dock=DockPose(x=-1.0, y=2.0, heading_deg=0.0),
)
FRONT_ROUTE = [(1.0, 1.0), (19.0, 1.0), (19.0, 5.0), (1.0, 5.0), (1.0, 9.0), (18.0, 9.0)]
BACK_ROUTE = [(1.0, 17.0), (19.0, 17.0), (19.0, 25.0), (1.0, 25.0)]


def _route(index: int | None, *, transfer: bool = False) -> MowPath:
    lines = [FRONT_ROUTE, BACK_ROUTE]
    kinds: list[str] = ["mow", "mow"]
    if transfer:
        lines = [FRONT_ROUTE, [(18.0, 10.0), (18.0, 16.0)], BACK_ROUTE]
        kinds = ["mow", "transfer", "mow"]
    return MowPath.model_validate(
        {
            "path_hash": "777",
            "polylines": lines,
            "kinds": kinds,
            "points_total": sum(len(line) for line in lines),
            "area_ids": ["11", "22"],
            "planned_at": T0,
            "progress": {"index": index} if index is not None else None,
        }
    )


def _state(
    activity: MowerActivity = MowerActivity.ERROR,
    *,
    x: float = 18.0,
    y: float = 9.2,
    theta: float | None = math.pi / 2,
    **changes: object,
) -> MowerState:
    values: dict[str, object] = {
        "mower_id": uuid4(),
        "account_id": uuid4(),
        "observed_at": T0,
        "activity": activity,
        "battery_percent": 64,
        "rtk_fix": RtkFixStatus.FIXED,
        "connection": ConnectionKind.SIMULATED,
        "position": to_geo_position(GARDEN, x, y, theta),
        **changes,
    }
    if activity is MowerActivity.ERROR:
        values.setdefault("error_code", "1201")
        values.setdefault("error_message", "The robot is stuck")
        values.setdefault("drive_recoverable", True)
    return MowerState.model_validate(values)


def _job(
    state: MowerState, path: MowPath | None, *, last_start: list[str] | None = None
) -> JobContext | None:
    """The job seen from the channel entry (18, 9.2), facing the path (+y)."""
    x, y = (18.0, 9.2)
    return job_context(
        state,
        mower_map=GARDEN,
        here=(x, y),
        theta_rad=math.pi / 2,
        path=path,
        last_start=last_start,
    )


def _resume(steps: list[ProposedStep] | None = None, **changes: object) -> AssessmentOut:
    values: dict[str, object] = {
        "situation": "stuck_on_obstacle",
        "confidence": 0.8,
        "action": "resume",
        "proposal": [s.model_dump() for s in steps or []],
        "rationale": "The gate is wide enough; continue the job.",
        **changes,
    }
    return AssessmentOut.model_validate(values)


# --- the job context -----------------------------------------------------------------------


def test_vision_job_channel_entry_from_the_mowers_route() -> None:
    # Stuck at the far end of the front, the front's route done, the back still to mow.
    job = _job(_state(), _route(len(FRONT_ROUTE)))
    assert job is not None
    assert (job.status, job.source, job.resumable) == ("paused_by_error", "mow_path", True)
    assert job.error_code == "1201"
    assert [(a.name, a.status) for a in job.areas] == [
        ("Vorderer Garten", "current"),
        ("Hinterer Garten", "next"),
    ]
    assert job.current_area == "Vorderer Garten" and job.current_area_finished is True
    assert job.next_area == "Hinterer Garten"
    assert job.progress_percent == 60
    link = job.connection_to_next
    assert link is not None and link.source == "map_path" and link.path_name == "Durchgang"
    assert (link.from_area, link.to_area) == ("Vorderer Garten", "Hinterer Garten")
    # The entry (18, 10) lies 0.8 m straight ahead of the mower facing +y.
    assert link.entry_distance_m == 0.8 and abs(link.entry_bearing_deg) < 1
    assert link.entry_forward == 0.8 and abs(link.entry_left) < 0.01
    assert link.mower_at_entry is True and link.entry_ahead is True
    assert "error cleared" in job.resume_note


def test_vision_job_progress_marks_done_current_next_and_planned() -> None:
    plan = MowPath.model_validate(
        {
            "path_hash": "1",
            "polylines": [FRONT_ROUTE, BACK_ROUTE, [(31.0, 1.0), (39.0, 9.0)]],
            "points_total": len(FRONT_ROUTE) + len(BACK_ROUTE) + 2,
            "area_ids": ["11", "22", "33"],
            "planned_at": T0,
            "progress": {"index": len(FRONT_ROUTE) + 1},
        }
    )
    here = (5.0, 20.0)  # in the back
    job = job_context(
        _state(MowerActivity.MOWING, x=here[0], y=here[1]),
        mower_map=GARDEN,
        here=here,
        theta_rad=0.0,
        path=plan,
        last_start=None,
    )
    assert job is not None and job.status == "running" and not job.resumable
    assert [a.status for a in job.areas] == ["done", "current", "next"]
    assert job.resume_note == "the job is running"
    assert job.connection_to_next is None, "no mapped path or transfer line leads to the side"


def test_vision_job_falls_back_to_the_route_transfer_line() -> None:
    no_paths = GARDEN.model_copy(update={"paths": []})
    job = job_context(
        _state(),
        mower_map=no_paths,
        here=(18.0, 9.2),
        theta_rad=math.pi / 2,
        path=_route(len(FRONT_ROUTE), transfer=True),
        last_start=None,
    )
    assert job is not None and job.connection_to_next is not None
    assert job.connection_to_next.source == "job_route"
    assert job.connection_to_next.mower_at_entry is True


def test_vision_job_from_the_newest_start_when_no_route_is_known() -> None:
    job = _job(_state(), None, last_start=["11", "22"])
    assert job is not None and job.source == "start_command" and job.progress_percent is None
    assert [a.status for a in job.areas] == ["current", "next"]
    assert job.connection_to_next is not None and job.connection_to_next.mower_at_entry
    back = job_context(
        _state(y=20.0),
        mower_map=GARDEN,
        here=(5.0, 20.0),
        theta_rad=None,
        path=None,
        last_start=["11", "22"],
    )
    assert back is not None
    assert [a.status for a in back.areas] == ["done", "current"], "done areas are inferred"
    assert back.next_area is None and back.connection_to_next is None


def test_vision_job_none_and_not_resumable_cases() -> None:
    assert _job(_state(MowerActivity.IDLE), _route(3)) is None, "no job while idle"
    assert _job(_state(MowerActivity.DOCKED), None) is None
    assert _job(_state(), None) is None, "an error without a route or a start: no job evidence"
    paused = _job(_state(MowerActivity.PAUSED), None)
    assert paused is not None and paused.source == "state" and paused.resumable
    assert paused.resume_note == "the job is paused; resume continues it"
    lifted = _job(_state(raw={"lifted": True}), _route(3))
    assert lifted is not None and not lifted.resumable and "lifted" in lifted.resume_note
    tilt = _job(_state(error_code="2801", error_message="Tilt sensor"), _route(3))
    assert tilt is not None and not tilt.resumable and "tilt" in tilt.resume_note
    safety = _job(_state(drive_recoverable=False), _route(3))
    assert safety is not None and not safety.resumable and "safety stop" in safety.resume_note
    spot = _job(
        _state(
            MowerActivity.MOWING,
            spot_run=SpotRunStatus(run_id=uuid4(), state="spot_mowing", progress_percent=40),
        ),
        None,
    )
    assert spot is not None and spot.status == "spot_run" and not spot.resumable
    returning = _job(_state(MowerActivity.RETURNING), _route(3))
    assert returning is not None and not returning.resumable
    assert resume_problem(None, _state()) == "no mowing job of the mower is known"
    assert resume_problem(None, None) == "no state of the mower is known"


def test_vision_job_is_in_the_scene_context() -> None:
    state = _state()
    context = build_context(
        VisionTask.RECOVER,
        state=state,
        state_age_seconds=1,
        mower_map=GARDEN,
        commands=[],
        now=T0,
        last_drive=None,
        previous_rounds=[],
        motion=MOTION,
        images=2,
        mow_path=_route(len(FRONT_ROUTE)),
    )
    assert context.job is not None and context.job.next_area == "Hinterer Garten"
    dumped = context.model_dump(mode="json")
    assert dumped["job"]["connection_to_next"]["mower_at_entry"] is True
    # The dock route still says return to dock applies: both options are on the table.
    assert context.dock_route is not None and context.dock_route.return_to_dock_applicable
    idle = build_context(
        VisionTask.RECOVER,
        state=_state(MowerActivity.IDLE),
        state_age_seconds=1,
        mower_map=GARDEN,
        commands=[],
        now=T0,
        last_drive=None,
        previous_rounds=[],
        motion=MOTION,
        images=1,
    )
    assert idle.job is None


# --- the rules -----------------------------------------------------------------------------


def test_vision_job_resume_is_proposed_only_for_a_resumable_job() -> None:
    state = _state()
    job = _job(state, _route(len(FRONT_ROUTE)))
    dock = dock_route(GARDEN, (18.0, 9.2), state)
    back_off = [ProposedStep(linear=-0.5, angular=0.0, seconds=1.0)]
    assert evaluate(_resume(back_off), min_confidence=0.6, dock=dock, job=job) == (
        ProposalState.PROPOSED,
        None,
    )
    decision = decide(_resume(back_off), min_confidence=0.6, dock=dock, job=job)
    assert decision.action is ProposalAction.RESUME and not decision.fallback
    assert decide(_resume(), min_confidence=0.6, dock=dock, job=job).action is (
        ProposalAction.RESUME
    ), "resume without steps is allowed"
    # No job: not applicable, the dock fallback steps in (labelled).
    no_job = decide(_resume(), min_confidence=0.6, dock=dock, job=None)
    assert no_job.action is ProposalAction.RETURN_TO_DOCK and no_job.fallback
    assert "resume is not applicable: no mowing job" in (no_job.detail or "")
    state_only, why = evaluate(_resume(), min_confidence=0.6, dock=None, job=None)
    assert state_only is ProposalState.REJECTED_BY_AI and "no mowing job" in (why or "")
    running = _job(_state(MowerActivity.MOWING), _route(2))
    assert evaluate(_resume(), min_confidence=0.6, job=running)[0] is (ProposalState.REJECTED_BY_AI)
    find_dock, why = evaluate(_resume(), min_confidence=0.6, job=job, task=VisionTask.FIND_DOCK)
    assert find_dock is ProposalState.REJECTED_BY_AI and "find_dock" in (why or "")
    # Risk flags and low confidence still win.
    risky = _resume(risk=["people_visible"])
    assert evaluate(risky, min_confidence=0.6, job=job)[0] is ProposalState.REJECTED_BY_AI
    unsure = _resume(confidence=0.4)
    assert evaluate(unsure, min_confidence=0.6, job=job)[0] is ProposalState.REJECTED_BY_AI


def test_vision_job_resume_gate_needs_the_error_cleared() -> None:
    assert resume_gate(None) == "no state of the mower is known"
    assert resume_gate(_state()) == "the mower still reports error 1201"
    assert resume_gate(_state(MowerActivity.PAUSED, raw={"lifted": True})) is not None
    assert resume_gate(_state(MowerActivity.OFFLINE)) == "the mower is offline"
    assert resume_gate(_state(MowerActivity.PAUSED)) is None


# --- prompt and schema ---------------------------------------------------------------------


def test_vision_job_prompt_prefers_resume_at_a_passable_connection() -> None:
    action = ASSESSMENT_SCHEMA["properties"]["action"]
    assert "resume" in action["enum"] and "resume" in action["description"]
    assert '"resume"' in PROMPT_MODE_SUFFIX, "prompt mode carries the same schema"
    assert "`resume`" in SYSTEM_PROMPT and "job.resumable" in SYSTEM_PROMPT
    assert "mower_at_entry" in SYSTEM_PROMPT and "connection_to_next" in SYSTEM_PROMPT
    # The owner's mower runs a sensitive obstacle setting: the images decide, not 1201 alone.
    assert "obstacle sensitivity" in SYSTEM_PROMPT and "1201" in SYSTEM_PROMPT
    assert "the images decide whether passing is safe" in SYSTEM_PROMPT
    assert "unsafe or impossible" in SYSTEM_PROMPT
    resume = AssessmentOut.model_validate(
        {"situation": "stuck_on_obstacle", "confidence": 0.7, "action": "resume"}
    )
    assert resume.action is ProposalAction.RESUME and resume.proposal == []


def test_vision_job_uses_the_mowers_own_job_report() -> None:
    from mowdeck_contracts.v1 import JobProgress

    # No route and no start seen (a restarted service), but the mower reports its job.
    reported = _state(job=JobProgress(progress_percent=42, area_id="11"))
    job = _job(reported, None)
    assert job is not None and job.status == "paused_by_error" and job.source == "state"
    assert job.current_area == "Vorderer Garten" and job.progress_percent == 42
    assert job.resumable
    # The mower's own area wins over the geometry (RTK noise at an area's edge).
    elsewhere = _state(job=JobProgress(progress_percent=70, area_id="22"))
    routed = _job(elsewhere, _route(len(FRONT_ROUTE)))
    assert routed is not None and routed.current_area == "Hinterer Garten"
    assert routed.progress_percent == 60, "the route's progress is finer than the report"
