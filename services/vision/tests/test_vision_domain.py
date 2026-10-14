"""Pure rules: answer validation and evaluation, start and abort rules, geofence,
verification, model families and base URL policies, the scene context."""

import math
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import (
    ConnectionKind,
    MowerActivity,
    MowerState,
    VisionTask,
)
from mowdeck_control import default_map
from mowdeck_control.geo import to_geo_position
from pydantic import ValidationError

from vision.domain.context import Motion, Pose, build_context, pose_of, to_mower_frame
from vision.domain.errors import InvalidInput
from vision.domain.families import check_api_base, check_model, family_of
from vision.domain.models import AssessmentOut, ProposalState, ProposedStep, RiskFlag, Situation
from vision.domain.rules import (
    Geofence,
    day_start,
    evaluate,
    frames_for,
    polygon_distance,
    start_problem,
    state_abort_reason,
    total_seconds,
    verify,
)

MAP = default_map()
T0 = datetime(2026, 10, 10, 8, 0, tzinfo=UTC)
MOTION = Motion(linear_mps_at_1=0.5, turn_deg_per_s_at_1=57)


def _state(
    activity: MowerActivity = MowerActivity.IDLE,
    *,
    x: float = 4.0,
    y: float = 3.0,
    theta: float = 0.0,
    **changes: object,
) -> MowerState:
    values: dict[str, object] = {
        "mower_id": uuid4(),
        "account_id": uuid4(),
        "observed_at": T0,
        "activity": activity,
        "battery_percent": 80,
        "connection": ConnectionKind.SIMULATED,
        "position": to_geo_position(MAP, x, y, theta),
        **changes,
    }
    return MowerState.model_validate(values)


def _answer(**changes: object) -> AssessmentOut:
    values: dict[str, object] = {
        "situation": "stuck_on_obstacle",
        "confidence": 0.8,
        "proposal": [{"linear": -0.5, "angular": 0, "seconds": 1}],
        "rationale": "x",
        **changes,
    }
    return AssessmentOut.model_validate(values)


def test_vision_domain_answer_validation() -> None:
    long = _answer(rationale="r" * 5000, abort_reasons=["a" * 500] * 9, risk=["water", "water"])
    assert len(long.rationale) == 1000
    assert len(long.abort_reasons) == 6 and len(long.abort_reasons[0]) == 200
    assert long.risk == [RiskFlag.WATER]
    for bad in (
        {"proposal": [{"linear": 0, "angular": 0, "seconds": 1}] * 7},
        {"proposal": [{"linear": 1.5, "angular": 0, "seconds": 1}]},
        {"proposal": [{"linear": 0, "angular": 0, "seconds": 3.5}]},
        {"proposal": [{"linear": 0, "angular": 0, "seconds": 1, "blade_on": True}]},
        {"confidence": 1.2},
        {"situation": "mars"},
        {"risk": ["lava"]},
    ):
        with pytest.raises(ValidationError):
            _answer(**bad)


def test_vision_domain_evaluate() -> None:
    assert evaluate(_answer(), min_confidence=0.6) == (ProposalState.PROPOSED, None)
    state, detail = evaluate(_answer(risk=["road", "drop_off"]), min_confidence=0.6)
    assert state is ProposalState.REJECTED_BY_AI and detail == "risk: drop_off, road"
    state, detail = evaluate(_answer(confidence=0.5), min_confidence=0.6)
    assert detail == "confidence 0.50 below 0.60"
    state, detail = evaluate(_answer(proposal=[], abort_reasons=["dark"]), min_confidence=0)
    assert (state, detail) == (ProposalState.REJECTED_BY_AI, "dark")


def test_vision_domain_frames_and_days() -> None:
    step = ProposedStep(linear=1, angular=0, seconds=1.05)
    assert (
        frames_for(step, 5) == 5
        and frames_for(ProposedStep(linear=0, angular=0, seconds=0.01), 5) == 1
    )
    assert total_seconds([step, step]) == pytest.approx(2.1)
    local = datetime(2026, 10, 10, 0, 30, tzinfo=UTC) + timedelta(hours=23)
    assert day_start(local) == datetime(2026, 10, 10, tzinfo=UTC)


def test_vision_domain_start_and_abort_rules() -> None:
    assert start_problem(VisionTask.RECOVER, None) == "no state of the mower is known"
    assert start_problem(VisionTask.RECOVER, _state()) is None
    stuck = _state(MowerActivity.ERROR, error_code="stuck", drive_recoverable=True)
    assert start_problem(VisionTask.RECOVER, stuck) is None
    assert "use recover first" in (start_problem(VisionTask.FIND_DOCK, stuck) or "")
    lifted = _state(MowerActivity.ERROR, error_code="lifted", raw={"lifted": True})
    assert "lifted" in (start_problem(VisionTask.RECOVER, lifted) or "")
    sensor = _state(MowerActivity.ERROR, error_code="1004")
    assert "cannot be driven free" in (start_problem(VisionTask.RECOVER, sensor) or "")
    assert start_problem(VisionTask.RECOVER, _state(MowerActivity.MOWING)) == "the mower is mowing"
    idle = _state()
    assert state_abort_reason(_state(), idle) is None
    assert state_abort_reason(lifted, stuck) == "sensor_stop: lifted"
    assert state_abort_reason(sensor, idle) == "sensor_stop: 1004"
    assert state_abort_reason(stuck, idle) == "new_error: stuck"
    assert state_abort_reason(stuck, stuck) is None
    assert state_abort_reason(_state(MowerActivity.OFFLINE), idle) == "offline"
    assert (
        state_abort_reason(_state(MowerActivity.RETURNING), idle) == "activity_changed: returning"
    )


def test_vision_domain_geofence() -> None:
    fence = Geofence(MAP, margin_m=2.0)
    assert fence.violation((4, 3)) is None
    assert fence.violation((-1.5, 1.0)) is None, "the dock surroundings"
    assert fence.violation((13.5, 3)) is None, "within the margin"
    assert fence.violation((20, 3)) == "geofence: outside the mapped areas"
    assert fence.violation((10.5, 7)) == "geofence: inside flowerbed"
    assert polygon_distance((15, 4), MAP.areas[0].boundary) == pytest.approx(3.0)
    assert polygon_distance((4, 4), MAP.areas[0].boundary) == 0.0
    assert polygon_distance((1, 1), [(0, 0), (0, 0), (0, 0)]) == pytest.approx(math.sqrt(2))


def test_vision_domain_verify() -> None:
    back = [ProposedStep(linear=-0.5, angular=0, seconds=1)]
    turn = [ProposedStep(linear=0, angular=0.5, seconds=1)]
    start, moved = Pose(x=4, y=3, theta_rad=0), Pose(x=3.6, y=3, theta_rad=0)
    ok, detail = verify(
        VisionTask.RECOVER, start=start, end=moved, end_state=_state(), steps=back, min_move_m=0.2
    )
    assert ok and detail.startswith("free again")
    ok, detail = verify(
        VisionTask.FIND_DOCK, start=start, end=moved, end_state=_state(), steps=back, min_move_m=0.2
    )
    assert ok and "returning to the dock" in detail
    still = _state(MowerActivity.ERROR, error_code="stuck", drive_recoverable=True)
    assert verify(
        VisionTask.RECOVER, start=start, end=moved, end_state=still, steps=back, min_move_m=0.2
    ) == (
        False,
        "the mower still reports stuck",
    )
    assert not verify(
        VisionTask.RECOVER, start=start, end=start, end_state=_state(), steps=back, min_move_m=0.2
    )[0]
    turned = Pose(x=4, y=3, theta_rad=0.5)
    assert verify(
        VisionTask.RECOVER, start=start, end=turned, end_state=_state(), steps=turn, min_move_m=0.2
    )[0]
    assert not verify(
        VisionTask.RECOVER, start=start, end=start, end_state=_state(), steps=turn, min_move_m=0.2
    )[0]
    unknown = Pose(x=4, y=3)
    assert (
        "heading is unknown"
        in verify(
            VisionTask.RECOVER,
            start=unknown,
            end=unknown,
            end_state=_state(),
            steps=turn,
            min_move_m=0.2,
        )[1]
    )
    assert (
        verify(
            VisionTask.RECOVER, start=None, end=None, end_state=_state(), steps=back, min_move_m=0.2
        )[0]
        is False
    )
    assert (
        verify(
            VisionTask.RECOVER, start=start, end=start, end_state=None, steps=back, min_move_m=0.2
        )[0]
        is False
    )


def test_vision_domain_families_and_base_urls() -> None:
    assert check_model("anthropic/claude-sonnet-5-5", allow_fake=False) == "anthropic"
    assert check_model("ollama_chat/llava:13b", allow_fake=False) == "ollama_chat"
    assert check_model("fake/recover", allow_fake=True) == "fake"
    assert family_of("openai/gpt-5-mini") == "openai"
    for bad in ("fake/recover", "claude", "Anthropic/x", "unknown/model", "openai/ space"):
        with pytest.raises(InvalidInput):
            check_model(bad, allow_fake=False)
    assert check_api_base(None, "public") is None and check_api_base("  ", "any") is None
    assert check_api_base("http://192.168.1.5:11434/", "any") == "http://192.168.1.5:11434"
    assert check_api_base("https://api.example.org/v1", "public") == "https://api.example.org/v1"
    for host in (
        "http://localhost:1",
        "http://10.0.0.1",
        "http://[::1]:8",
        "http://ollama",
        "http://nas.local",
    ):
        with pytest.raises(InvalidInput):
            check_api_base(host, "public")
    with pytest.raises(InvalidInput):
        check_api_base("https://x.example.org/" + "a" * 300, "any")


def test_vision_domain_scene_context_in_the_mower_frame() -> None:
    state = _state(x=4.0, y=3.0, theta=math.pi / 2)  # facing +y in the map frame
    pose = pose_of(state, MAP)
    assert pose is not None and pose.x == pytest.approx(4.0, abs=1e-6)
    assert pose.theta_rad is not None and math.cos(pose.theta_rad) == pytest.approx(0, abs=1e-6)
    forward, left = to_mower_frame((4.0, 5.0), pose)
    assert forward == pytest.approx(2.0)
    assert left == pytest.approx(0.0, abs=0.01)
    context = build_context(
        VisionTask.FIND_DOCK,
        state=state,
        state_age_seconds=1.24,
        mower_map=MAP,
        commands=[("start_mow", T0 - timedelta(seconds=30)), ("manual_drive", T0)],
        now=T0,
        last_drive=None,
        previous_rounds=[],
        motion=MOTION,
        images=1,
        radius_m=8,
    )
    assert context.mower is not None and context.mower.seconds_old == 1.2
    assert context.map is not None and context.map.inside_area == "front"
    dock = context.map.dock
    assert dock is not None and dock.distance_m == pytest.approx(math.hypot(5, 2), abs=0.01)
    assert dock.bearing_deg > 90, "the dock is behind-left of a mower facing +y at (4, 3)"
    assert {s.id for s in context.map.shapes} >= {"front", "flowerbed"}
    assert [c.seconds_ago for c in context.recent_commands] == [30.0, 0.0]
    blind = build_context(
        VisionTask.RECOVER,
        state=None,
        state_age_seconds=0,
        mower_map=None,
        commands=[],
        now=T0,
        last_drive="AI assist round 1: aborted",
        previous_rounds=[],
        motion=MOTION,
        images=0,
    )
    assert blind.mower is None and blind.map is None
    assert pose_of(_state(position=None), MAP) is None
    assert Situation("edge") is Situation.EDGE


def test_vision_domain_verify_counts_the_travelled_path() -> None:
    back = [ProposedStep(linear=-0.5, angular=0, seconds=1)]
    start = Pose(x=4, y=3, theta_rad=0)
    near = Pose(x=4.05, y=3.05, theta_rad=0.5)
    ok, detail = verify(
        VisionTask.RECOVER,
        start=start,
        end=near,
        end_state=_state(),
        steps=back,
        min_move_m=0.2,
        travelled_m=0.7,
    )
    assert (ok, detail) == (True, "free again, moved 0.70 m")
