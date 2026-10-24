"""A recommendation below the confidence threshold (ADR 0033 addendum 2026-10-24): kept as a
low-confidence option next to the dock fallback when it passes the safety checks, refused with
the reason when it does not, unchanged above the threshold; the planned path check; the
approval with each option end to end (the live case of 2026-10-24: a LUBA stuck with error 1201
at the entry of the transfer path, the model 55 % sure that resume works)."""

import math
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from mowdeck_contracts.v1 import (
    VISION_PROPOSAL_CREATED,
    ConnectionKind,
    DockPose,
    EventEnvelope,
    GeoReference,
    ManualDrive,
    MapArea,
    MapZone,
    MowerActivity,
    MowerMap,
    MowerState,
    RtkFixStatus,
    VisionTask,
)
from mowdeck_contracts.v1.subjects import event_subject_for_type
from mowdeck_control.geo import to_geo_position
from test_vision_resume import (
    DONE,
    FakeFactory,
    _kinds,
    _mowing_two_areas,
    _request,
    _stuck_at_the_side_path,
)
from vision_support import API, harness, wait_for

from vision.domain.context import Motion, Pose
from vision.domain.dock import dock_route
from vision.domain.job import JobContext
from vision.domain.models import (
    AssessmentOut,
    ProposalAction,
    ProposalState,
    ProposedStep,
    RiskFlag,
)
from vision.domain.rules import FALLBACK_PREFIX, Geofence, decide, path_problem

T0 = datetime(2026, 10, 24, 12, 17, tzinfo=UTC)
MOTION = Motion(linear_mps_at_1=0.5, turn_deg_per_s_at_1=57)
GARDEN = MowerMap(
    reference=GeoReference(origin_lat=49.7, origin_lon=11.7, rotation_deg=0.0),
    areas=[MapArea(id="a", name="Front", boundary=[(0, 0), (20, 0), (20, 10), (0, 10)])],
    no_go_zones=[
        MapZone(id="bed", name="Flower bed", boundary=[(12, 4), (14, 4), (14, 6), (12, 6)])
    ],
    dock=DockPose(x=-2.0, y=5.0, heading_deg=0.0),
)
FENCE = Geofence(GARDEN, margin_m=2.0)
JOB = JobContext(
    status="paused_by_error",
    source="state",
    resumable=True,
    resume_note="the job is paused by the error",
)
LIVE_STEPS = [
    ProposedStep(linear=-0.5, angular=0.0, seconds=1.5),
    ProposedStep(linear=0.0, angular=-0.3, seconds=1.0),
    ProposedStep(linear=0.5, angular=0.0, seconds=1.5),
]
"""What the model answered at 12:17 UTC: back 1.5 s, turn -0.3 for 1 s, forward 1.5 s."""


def _state(x: float = 10.0, y: float = 5.0, theta: float = 0.0) -> MowerState:
    return MowerState.model_validate(
        {
            "mower_id": uuid4(),
            "account_id": uuid4(),
            "observed_at": T0,
            "activity": MowerActivity.ERROR,
            "error_code": "1201",
            "drive_recoverable": True,
            "battery_percent": 80,
            "rtk_fix": RtkFixStatus.FIXED,
            "connection": ConnectionKind.SIMULATED,
            "position": to_geo_position(GARDEN, x, y, theta),
        }
    )


def _route() -> Any:
    return dock_route(GARDEN, (10.0, 5.0), _state())


def _unsure(**changes: object) -> AssessmentOut:
    values: dict[str, object] = {
        "situation": "stuck_on_obstacle",
        "confidence": 0.55,
        "action": "resume",
        "proposal": [s.model_dump() for s in LIVE_STEPS],
        "rationale": "A short back-up and a gentle move forward, then the job continues.",
        "abort_reasons": ["stop if the mower touches the wall"],
        **changes,
    }
    return AssessmentOut.model_validate(values)


def _fine(steps: object) -> str | None:
    return None


# --- the decision ----------------------------------------------------------------------------


def test_low_confidence_resume_is_offered_next_to_the_fallback() -> None:
    decision = decide(_unsure(), min_confidence=0.6, dock=_route(), job=JOB, path_check=_fine)
    assert decision.state is ProposalState.PROPOSED
    # The fallback stays the default (what old clients approve), the model's resume is offered.
    assert decision.action is ProposalAction.RETURN_TO_DOCK and decision.fallback
    assert decision.alternative is ProposalAction.RESUME and decision.low_confidence
    assert decision.model_refused is None
    assert decision.detail is not None and decision.detail.startswith(FALLBACK_PREFIX)
    assert "recommends resume" in decision.detail and "0.55 below 0.60" in decision.detail
    assert "recommended nothing" not in decision.detail


def test_low_confidence_drive_alone_when_no_fallback_applies() -> None:
    drive = _unsure(action="drive")
    alone = decide(drive, min_confidence=0.6, dock=None, path_check=_fine)
    assert alone.state is ProposalState.PROPOSED and not alone.fallback
    assert alone.action is ProposalAction.DRIVE and alone.low_confidence
    assert alone.alternative is None
    tried = decide(drive, min_confidence=0.6, dock=_route(), allow_fallback=False, path_check=_fine)
    assert tried.action is ProposalAction.DRIVE and not tried.fallback and tried.low_confidence


def test_low_confidence_return_to_dock_is_one_option() -> None:
    dock = _unsure(action="return_to_dock", proposal=[])
    decision = decide(dock, min_confidence=0.6, dock=_route())
    assert decision.state is ProposalState.PROPOSED
    assert decision.action is ProposalAction.RETURN_TO_DOCK
    assert not decision.fallback and decision.low_confidence and decision.alternative is None


def test_low_confidence_failing_the_path_check_is_not_offered() -> None:
    def leaves(steps: object) -> str:
        return "step 3 leaves the safe area (geofence: inside bed)"

    decision = decide(_unsure(), min_confidence=0.6, dock=_route(), job=JOB, path_check=leaves)
    assert decision.state is ProposalState.PROPOSED and decision.fallback
    assert decision.alternative is None and not decision.low_confidence
    assert decision.model_refused == "step 3 leaves the safe area (geofence: inside bed)"
    assert decision.detail is not None and "is not offered" in decision.detail
    # Without a path check, steps below the threshold are never offered.
    unchecked = decide(_unsure(), min_confidence=0.6, dock=_route(), job=JOB)
    assert unchecked.model_refused == "the planned path cannot be checked"
    # Without the fallback nothing is left: rejected, with the reason.
    rejected = decide(_unsure(action="drive"), min_confidence=0.6, dock=None, path_check=leaves)
    assert rejected.state is ProposalState.REJECTED_BY_AI
    assert rejected.model_refused is not None and "inside bed" in (rejected.detail or "")


def test_low_confidence_resume_without_a_paused_job_is_not_offered() -> None:
    decision = decide(_unsure(), min_confidence=0.6, dock=_route(), job=None, path_check=_fine)
    assert decision.fallback and decision.alternative is None
    assert decision.model_refused is not None and "resume is not applicable" in (
        decision.model_refused
    )


def test_low_confidence_with_a_risk_flag_offers_nothing() -> None:
    risky = _unsure(risk=[RiskFlag.PEOPLE_VISIBLE])
    decision = decide(risky, min_confidence=0.6, dock=_route(), job=JOB, path_check=_fine)
    assert decision.state is ProposalState.REJECTED_BY_AI
    assert not decision.fallback and decision.alternative is None


def test_above_the_threshold_nothing_changes() -> None:
    sure = _unsure(confidence=0.8)
    decision = decide(sure, min_confidence=0.6, dock=_route(), job=JOB)
    assert decision.state is ProposalState.PROPOSED
    assert decision.action is ProposalAction.RESUME
    assert not decision.fallback and not decision.low_confidence and decision.alternative is None
    assert decision.detail is None


def test_no_recommendation_still_reads_as_none() -> None:
    none = _unsure(action="none", proposal=[], confidence=0.4)
    decision = decide(none, min_confidence=0.6, dock=_route(), path_check=_fine)
    assert decision.fallback and decision.alternative is None and not decision.low_confidence
    assert decision.detail is not None and "The model recommended nothing" in decision.detail
    # A sure recommendation that is not applicable is not called "nothing".
    sure = _unsure(confidence=0.8)
    refused = decide(sure, min_confidence=0.6, dock=_route(), job=None)
    assert refused.fallback and refused.detail is not None
    assert "The model's resume is not offered" in refused.detail


# --- the planned path ------------------------------------------------------------------------


def test_path_check_accepts_the_live_maneuver_inside_the_area() -> None:
    pose = Pose(x=8.0, y=5.0, theta_rad=math.pi)
    assert path_problem(LIVE_STEPS, pose=pose, fence=FENCE, motion=MOTION) is None


def test_path_check_refuses_a_no_go_zone_and_leaving_the_geofence() -> None:
    towards_bed = [ProposedStep(linear=1.0, angular=0.0, seconds=3.0)]
    pose = Pose(x=11.0, y=5.0, theta_rad=0.0)
    problem = path_problem(towards_bed, pose=pose, fence=FENCE, motion=MOTION)
    assert problem is not None and "step 1" in problem and "inside bed" in problem
    out = Pose(x=19.5, y=5.0, theta_rad=0.0)
    far = [ProposedStep(linear=1.0, angular=0.0, seconds=3.0)] * 3
    problem = path_problem(far, pose=out, fence=FENCE, motion=MOTION)
    assert problem is not None and "outside the mapped areas" in problem


def test_path_check_needs_map_position_and_heading() -> None:
    pose = Pose(x=8.0, y=5.0, theta_rad=None)
    assert "heading" in (path_problem(LIVE_STEPS, pose=pose, fence=FENCE, motion=MOTION) or "")
    turn_only = [ProposedStep(linear=0.0, angular=0.5, seconds=1.0)]
    assert path_problem(turn_only, pose=pose, fence=FENCE, motion=MOTION) is None
    assert "position" in (path_problem(LIVE_STEPS, pose=None, fence=FENCE, motion=MOTION) or "")
    assert "no map" in (path_problem(LIVE_STEPS, pose=pose, fence=None, motion=MOTION) or "")
    assert path_problem([], pose=None, fence=None, motion=MOTION) is None


# --- end to end --------------------------------------------------------------------------------


def _created(h: Any) -> list[dict[str, Any]]:
    return [
        EventEnvelope[dict[str, Any]].model_validate_json(m.data).payload
        for m in h.bus.published
        if m.subject == event_subject_for_type(VISION_PROPOSAL_CREATED)
    ]


async def test_low_confidence_resume_end_to_end_with_the_model_option() -> None:
    factory = FakeFactory(model="fake/unsure_resume")
    async with harness(factory=factory) as h:
        owner = h.owner()
        rig = await _mowing_two_areas(h, owner)
        await _stuck_at_the_side_path(h, rig)
        proposal_id = await _request(h, owner, rig)
        proposal = await h.until(proposal_id, owner, ProposalState.PROPOSED, *DONE)
        assert proposal["state"] == "proposed", proposal["detail"]
        assert proposal["action"] == "return_to_dock" and proposal["fallback"] is True
        assert proposal["alternative_action"] == "resume" and proposal["low_confidence"]
        assert proposal["assessment"]["confidence"] == 0.55
        options = proposal["options"]
        assert [(o["option"], o["action"], o["default"]) for o in options] == [
            ("fallback", "return_to_dock", True),
            ("model", "resume", False),
        ]
        assert options[1]["low_confidence"] and options[1]["confidence"] == 0.55
        assert [s["linear"] for s in options[1]["steps"]] == [-0.8]
        # The realtime summary (HACS reads `action`): the default plus the alternative.
        [event] = _created(h)
        assert event["action"] == "return_to_dock" and event["fallback"] is True
        assert event["alternative_action"] == "resume" and event["low_confidence"] is True
        # Nothing ran before the approval; a fallback-only choice is refused for nothing.
        assert "manual_drive" not in _kinds(rig) and "resume_mow" not in _kinds(rig)
        approved = await h.client.post(
            f"{API}/proposals/{proposal_id}/approve",
            json={"option": "model"},
            headers=owner.headers,
        )
        assert approved.status_code == 200, approved.text
        body = approved.json()
        assert body["action"] == "resume" and body["approved_option"] == "model"
        assert body["fallback"] is False and body["low_confidence"] is True
        assert [s["linear"] for s in body["steps"]] == [-0.8]
        done = await h.until(proposal_id, owner, *DONE, timeout=20)
        assert done["state"] == "recovered", done["detail"]
        # The model's maneuver ran like any drive (blades off, through Fleet), then resume_mow.
        frames = [c.command for c in rig.commands if isinstance(c.command, ManualDrive)]
        assert frames and all(not f.blade_on for f in frames)
        [start] = h.fleet.starts
        assert start.blade_on_allowed is False
        [issued] = h.fleet.issued
        assert issued.command.command.kind == "resume_mow"
        assert "return_to_dock" not in _kinds(rig)
        await wait_for(lambda: rig.sim.snapshot().activity is MowerActivity.MOWING)


async def test_low_confidence_resume_end_to_end_with_the_fallback() -> None:
    factory = FakeFactory(model="fake/unsure_resume")
    async with harness(factory=factory) as h:
        owner = h.owner()
        rig = await _mowing_two_areas(h, owner)
        await _stuck_at_the_side_path(h, rig)
        proposal_id = await _request(h, owner, rig)
        await h.until(proposal_id, owner, ProposalState.PROPOSED, *DONE)
        # An option the proposal does not offer is refused, the proposal stays open.
        wrong = await h.client.post(
            f"{API}/proposals/{proposal_id}/approve",
            json={"option": "nonsense"},
            headers=owner.headers,
        )
        assert wrong.status_code == 422
        approved = await h.client.post(
            f"{API}/proposals/{proposal_id}/approve",
            json={"option": "fallback"},
            headers=owner.headers,
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["approved_option"] == "fallback"
        done = await h.until(proposal_id, owner, *DONE, timeout=20)
        assert done["state"] == "recovered", done["detail"]
        assert done["action"] == "return_to_dock"
        [issued] = h.fleet.issued
        assert issued.command.command.kind == "return_to_dock"
        assert "manual_drive" not in _kinds(rig) and "resume_mow" not in _kinds(rig)


async def test_low_confidence_default_approval_runs_the_fallback() -> None:
    factory = FakeFactory(model="fake/unsure_resume")
    async with harness(factory=factory) as h:
        owner = h.owner()
        rig = await _mowing_two_areas(h, owner)
        await _stuck_at_the_side_path(h, rig)
        proposal_id = await _request(h, owner, rig)
        await h.until(proposal_id, owner, ProposalState.PROPOSED, *DONE)
        # An older client approves without a body: the default option, the fallback.
        approved = await h.client.post(
            f"{API}/proposals/{proposal_id}/approve", headers=owner.headers
        )
        assert approved.status_code == 200, approved.text
        done = await h.until(proposal_id, owner, *DONE, timeout=20)
        assert done["action"] == "return_to_dock"
        assert "resume_mow" not in _kinds(rig)


async def test_option_the_proposal_does_not_offer_is_refused() -> None:
    factory = FakeFactory(model="fake/resume")
    async with harness(factory=factory) as h:
        owner = h.owner()
        rig = await _mowing_two_areas(h, owner)
        await _stuck_at_the_side_path(h, rig)
        proposal_id = await _request(h, owner, rig)
        proposal = await h.until(proposal_id, owner, ProposalState.PROPOSED, *DONE)
        assert proposal["low_confidence"] is False and proposal["alternative_action"] is None
        assert [o["option"] for o in proposal["options"]] == ["model"]
        refused = await h.client.post(
            f"{API}/proposals/{proposal_id}/approve",
            json={"option": "fallback"},
            headers=owner.headers,
        )
        assert refused.status_code == 422, refused.text
        assert (await h.proposal(proposal_id, owner))["state"] == "proposed"
        assert VisionTask.RECOVER.value == proposal["task"]


def test_prompt_no_longer_waits_for_the_stuck_error_to_clear() -> None:
    from vision.adapters.providers.prompt import SYSTEM_PROMPT
    from vision.domain.job import JobContext as Job

    assert "error cleared" not in SYSTEM_PROMPT
    assert "does not clear after" in SYSTEM_PROMPT
    assert "never make the plan or" in SYSTEM_PROMPT
    assert "honest estimate" in SYSTEM_PROMPT
    status = Job.model_fields["status"].description or ""
    assert "clears only once the resumed job moved" in status
