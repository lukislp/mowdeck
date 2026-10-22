"""Return to dock as a recovery option: the route check over the generic map, the decision with
the dock fallback, the scene context the model gets, the map image, the fake provider modes and
the flow through the API (offered, approved, executed as the normal dock command; nothing on a
lift)."""

import asyncio
import io
import math
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import (
    VISION_PROPOSAL_CREATED,
    ConnectionKind,
    DockPose,
    EventEnvelope,
    GeoReference,
    ManualDrive,
    MapArea,
    MapPath,
    MapZone,
    MowerActivity,
    MowerMap,
    MowerState,
    ReturnToDock,
    RtkFixStatus,
    VisionTask,
)
from mowdeck_contracts.v1.subjects import event_subject_for_type
from mowdeck_control.geo import to_geo_position
from PIL import Image as PILImage
from vision_support import API, StaticFactory, answer, harness, wait_for

from vision.adapters.providers.fake import FakeVisionProvider
from vision.adapters.providers.prompt import ASSESSMENT_SCHEMA, SYSTEM_PROMPT, user_content
from vision.domain.context import Motion, Pose, build_context
from vision.domain.dock import dock_route, hardware_fault
from vision.domain.mapimage import DOCK, MAP_IMAGE_SIZE, MOWER, half_span, render_map
from vision.domain.models import (
    AssessmentOut,
    Image,
    ProposalAction,
    ProposalState,
    RiskFlag,
    Situation,
)
from vision.domain.rules import FALLBACK_PREFIX, decide

T0 = datetime(2026, 10, 18, 14, 0, tzinfo=UTC)
MOTION = Motion(linear_mps_at_1=0.5, turn_deg_per_s_at_1=57)

GARDEN = MowerMap(
    reference=GeoReference(origin_lat=49.7, origin_lon=11.7, rotation_deg=0.0),
    areas=[
        MapArea(id="a", name="Front", boundary=[(0, 0), (20, 0), (20, 10), (0, 10)]),
        MapArea(id="b", name="Back", boundary=[(0, 14), (20, 14), (20, 24), (0, 24)]),
        MapArea(id="c", name="Island", boundary=[(40, 0), (50, 0), (50, 10), (40, 10)]),
    ],
    obstacles=[MapZone(id="tree", name="Tree", boundary=[(14, 6), (16, 6), (16, 8), (14, 8)])],
    paths=[MapPath(id="p1", points=[(10, 10), (10, 14)])],
    # Like most docks: at the lawn's edge, two metres outside the area.
    dock=DockPose(x=-2.0, y=5.0, heading_deg=0.0),
)
"""``Front`` holds the dock, ``Back`` is linked to it by a transfer path, ``Island`` is not."""


def _state(
    activity: MowerActivity = MowerActivity.PAUSED,
    *,
    x: float = 10.0,
    y: float = 5.0,
    theta: float = 0.0,
    **changes: object,
) -> MowerState:
    values: dict[str, object] = {
        "mower_id": uuid4(),
        "account_id": uuid4(),
        "observed_at": T0,
        "activity": activity,
        "battery_percent": 98,
        "rtk_fix": RtkFixStatus.FIXED,
        "connection": ConnectionKind.SIMULATED,
        "position": to_geo_position(GARDEN, x, y, theta),
        **changes,
    }
    return MowerState.model_validate(values)


def _route(state: MowerState, x: float, y: float) -> Any:
    return dock_route(GARDEN, (x, y), state)


def _declined(**changes: object) -> AssessmentOut:
    values: dict[str, object] = {
        "situation": "away_from_dock",
        "confidence": 0.7,
        "action": "none",
        "proposal": [],
        "rationale": "Recovery is not recommended.",
        "abort_reasons": ["a safe route cannot be verified"],
        **changes,
    }
    return AssessmentOut.model_validate(values)


# --- the route check -----------------------------------------------------------------------


def test_vision_dock_same_area_is_applicable() -> None:
    route = _route(_state(), 10, 5)
    assert route.return_to_dock_applicable
    assert (route.mower_area, route.dock_area, route.connected) == ("Front", "Front", True)
    assert route.via_paths == 0 and route.route == ["Front"]
    assert route.dock_distance_m == 12.0
    assert route.reason == "the mower is in the dock's area"


def test_vision_dock_area_linked_by_a_path_is_applicable() -> None:
    route = _route(_state(y=20), 10, 20)
    assert route.return_to_dock_applicable
    assert route.route == ["Back", "Front"] and route.via_paths == 1
    assert "transfer paths" in route.reason


def test_vision_dock_disconnected_area_is_not_applicable() -> None:
    route = _route(_state(x=45), 45, 5)
    assert not route.return_to_dock_applicable
    assert route.mower_area == "Island" and not route.connected
    assert route.reason == "no mapped path links the mower's area to the dock"


def test_vision_dock_outside_the_map_and_in_obstacles_is_not_applicable() -> None:
    edge = _route(_state(x=20.6), 20.6, 5)
    assert edge.return_to_dock_applicable and edge.mower_area == "Front", "RTK noise at the edge"
    outside = _route(_state(x=30), 30, 5)
    assert not outside.return_to_dock_applicable
    assert "outside the mapped areas" in outside.reason
    on_path = _route(_state(y=12), 10, 12)
    assert on_path.return_to_dock_applicable, "a mower on a transfer path may go home"
    in_tree = _route(_state(x=15, y=7), 15, 7)
    assert not in_tree.return_to_dock_applicable and "Tree" in in_tree.reason
    assert not dock_route(GARDEN, None, _state()).return_to_dock_applicable
    no_dock = GARDEN.model_copy(update={"dock": None})
    assert dock_route(no_dock, (10, 5), _state()).reason == "no map with a dock is known"


@pytest.mark.parametrize(
    ("changes", "words"),
    [
        ({"raw": {"lifted": True}}, "lifted"),
        (
            {"activity": MowerActivity.ERROR, "error_code": "lifted", "drive_recoverable": False},
            "lift",
        ),
        (
            {"activity": MowerActivity.ERROR, "error_code": "1005", "drive_recoverable": False},
            "safety stop",
        ),
        (
            {
                "activity": MowerActivity.ERROR,
                "error_code": "9001",
                "error_message": "Blade motor overcurrent",
                "drive_recoverable": True,
            },
            "blade or motor",
        ),
        ({"activity": MowerActivity.DOCKED}, "already docked"),
        ({"activity": MowerActivity.RETURNING}, "already returning"),
        ({"activity": MowerActivity.OFFLINE}, "offline"),
    ],
)
def test_vision_dock_blocking_states(changes: dict[str, object], words: str) -> None:
    activity = changes.pop("activity", MowerActivity.PAUSED)
    state = _state(activity, **changes)  # type: ignore[arg-type]
    route = _route(state, 10, 5)
    assert not route.return_to_dock_applicable
    assert words in route.reason


def test_vision_dock_paused_and_recoverable_errors_allow_it() -> None:
    paused = _state(MowerActivity.PAUSED)
    assert paused.drive_recoverable is False, "a paused mower reports no recoverable error"
    assert hardware_fault(paused) is None
    assert _route(paused, 10, 5).return_to_dock_applicable
    trapped = _state(
        MowerActivity.ERROR, error_code="1201", error_message="trapped", drive_recoverable=True
    )
    assert _route(trapped, 10, 5).return_to_dock_applicable


# --- decision and dock fallback -------------------------------------------------------------


def test_vision_dock_decline_with_applicable_route_offers_the_fallback() -> None:
    route = _route(_state(), 10, 5)
    decision = decide(_declined(), min_confidence=0.6, dock=route)
    assert decision.state is ProposalState.PROPOSED
    assert decision.action is ProposalAction.RETURN_TO_DOCK and decision.fallback
    assert decision.detail is not None and decision.detail.startswith(FALLBACK_PREFIX)
    assert "a safe route cannot be verified" in decision.detail
    low = decide(_declined(confidence=0.2), min_confidence=0.6, dock=route)
    assert low.fallback, "the fallback does not depend on the model's confidence"
    again = decide(_declined(), min_confidence=0.6, dock=route, allow_fallback=False)
    assert again.state is ProposalState.REJECTED_BY_AI, "an earlier round already tried it"


def test_vision_dock_no_fallback_when_disconnected_risky_or_lifted() -> None:
    island = _route(_state(x=45), 45, 5)
    rejected = decide(_declined(), min_confidence=0.6, dock=island)
    assert rejected.state is ProposalState.REJECTED_BY_AI and not rejected.fallback
    route = _route(_state(), 10, 5)
    people = decide(_declined(risk=[RiskFlag.PEOPLE_VISIBLE]), min_confidence=0.6, dock=route)
    assert people.state is ProposalState.REJECTED_BY_AI and not people.fallback
    lifted = _route(_state(raw={"lifted": True}), 10, 5)
    asked = _declined(action="return_to_dock", confidence=0.9)
    refused = decide(asked, min_confidence=0.6, dock=lifted)
    assert refused.state is ProposalState.REJECTED_BY_AI and not refused.fallback
    assert refused.detail is not None and "not applicable" in refused.detail
    assert decide(_declined(), min_confidence=0.6, dock=None).state is (
        ProposalState.REJECTED_BY_AI
    )


def test_vision_dock_model_choice_return_to_dock_is_proposed_without_fallback() -> None:
    route = _route(_state(), 10, 5)
    chosen = decide(
        _declined(action="return_to_dock", confidence=0.9), min_confidence=0.6, dock=route
    )
    assert chosen.state is ProposalState.PROPOSED
    assert chosen.action is ProposalAction.RETURN_TO_DOCK and not chosen.fallback
    drive = decide(answer(), min_confidence=0.6, dock=route)
    assert drive.action is ProposalAction.DRIVE and drive.state is ProposalState.PROPOSED


def test_vision_dock_answers_without_action_keep_working() -> None:
    legacy = AssessmentOut.model_validate(
        {"situation": "clear", "confidence": 0.5, "proposal": [], "rationale": ""}
    )
    assert legacy.action is ProposalAction.NONE
    steps = AssessmentOut.model_validate(
        {
            "situation": "clear",
            "confidence": 0.5,
            "proposal": [{"linear": 0.2, "angular": 0, "seconds": 1}],
        }
    )
    assert steps.action is ProposalAction.DRIVE


# --- what the model gets --------------------------------------------------------------------


def test_vision_dock_context_has_route_paths_track_and_no_misleading_flags() -> None:
    state = _state(MowerActivity.PAUSED, x=10, y=5, theta=0.0)
    track = [
        (*_latlon(6.0, 5.0), T0 - timedelta(seconds=60)),
        (*_latlon(8.0, 5.0), T0 - timedelta(seconds=30)),
    ]
    context = build_context(
        VisionTask.FIND_DOCK,
        state=state,
        state_age_seconds=7.4,
        mower_map=GARDEN,
        commands=[],
        now=T0,
        last_drive=None,
        previous_rounds=[],
        motion=MOTION,
        images=3,
        radius_m=8.0,
        track=track,
        map_image=True,
    )
    assert context.mower is not None
    assert context.mower.drive_recoverable is None, "only reported with an error"
    assert context.mower.sensor_stop is False
    assert context.dock_route is not None and context.dock_route.return_to_dock_applicable
    assert context.dock_route.mower_area == "Front"
    assert context.map is not None
    assert any(s.kind == "path" and s.id == "p1" for s in context.map.shapes)
    assert [round(p.forward) for p in context.recent_track] == [-4, -2]
    assert [p.seconds_ago for p in context.recent_track] == [60.0, 30.0]
    assert context.map_image is True
    dumped = context.model_dump_json(exclude_none=True)
    assert '"return_to_dock_applicable":true' in dumped
    lifted = build_context(
        VisionTask.RECOVER,
        state=_state(MowerActivity.ERROR, error_code="lifted", raw={"lifted": True}),
        state_age_seconds=0,
        mower_map=GARDEN,
        commands=[],
        now=T0,
        last_drive=None,
        previous_rounds=[],
        motion=MOTION,
        images=1,
    )
    assert lifted.mower is not None and lifted.mower.sensor_stop is True
    assert lifted.dock_route is not None and not lifted.dock_route.return_to_dock_applicable


def _latlon(x: float, y: float) -> tuple[float, float]:
    position = to_geo_position(GARDEN, x, y, None)
    return position.lat, position.lon


def test_vision_dock_prompt_and_schema_offer_return_to_dock() -> None:
    action = ASSESSMENT_SCHEMA["properties"]["action"]
    assert set(action["enum"]) == {"drive", "return_to_dock", "resume", "none"}
    assert "action" in ASSESSMENT_SCHEMA["required"]
    assert "`return_to_dock`" in SYSTEM_PROMPT and "lowest-risk" in SYSTEM_PROMPT
    assert "sensor_stop" in SYSTEM_PROMPT and "map image" in SYSTEM_PROMPT
    png = render_map(GARDEN, Pose(x=10, y=5, theta_rad=0.0))
    assert png is not None
    context = build_context(
        VisionTask.FIND_DOCK,
        state=_state(),
        state_age_seconds=0,
        mower_map=GARDEN,
        commands=[],
        now=T0,
        last_drive=None,
        previous_rounds=[],
        motion=MOTION,
        images=1,
        map_image=True,
    )
    parts = user_content(
        [Image(png, "image/png", source="map", camera="Map")], context, VisionTask.FIND_DOCK
    )
    assert parts[1]["text"].startswith("Image 1: Top-down map around the mower, heading up")
    assert parts[2]["image_url"]["url"].startswith("data:image/png;base64,")


# --- the map image ---------------------------------------------------------------------------


def _decode(png: bytes) -> Any:
    image = PILImage.open(io.BytesIO(png))
    assert image.format == "PNG" and image.size == (MAP_IMAGE_SIZE, MAP_IMAGE_SIZE)
    return image.convert("RGB")


def test_vision_dock_map_image_puts_mower_and_dock_where_they_are() -> None:
    pose = Pose(x=10.0, y=5.0, theta_rad=0.0)  # facing +x: the dock is 12 m behind
    png = render_map(GARDEN, pose)
    assert png is not None and len(png) < 60_000
    image = _decode(png)
    centre = MAP_IMAGE_SIZE // 2
    assert image.getpixel((centre, centre)) == MOWER
    scale = MAP_IMAGE_SIZE / (2 * half_span(GARDEN, pose))
    behind = round(centre + 12 * scale)
    assert image.getpixel((centre, behind)) == DOCK, "behind the mower is the bottom edge"
    # Facing +y the dock (at -x) is to the mower's left: the left half of the image.
    turned = _decode(render_map(GARDEN, Pose(x=10.0, y=5.0, theta_rad=math.pi / 2)) or b"")
    assert turned.getpixel((round(centre - 12 * scale), centre)) == DOCK


def test_vision_dock_map_image_points_to_a_far_dock_on_the_edge() -> None:
    pose = Pose(x=45.0, y=5.0, theta_rad=0.0)
    image = _decode(render_map(GARDEN, pose) or b"")
    blue = [
        (x, y)
        for x in range(MAP_IMAGE_SIZE)
        for y in range(0, MAP_IMAGE_SIZE, 2)
        if image.getpixel((x, y)) == DOCK
    ]
    assert blue, "an arrow towards the dock"
    assert all(abs(x - MAP_IMAGE_SIZE // 2) < 40 for x, _ in blue), "straight behind"
    assert all(y > MAP_IMAGE_SIZE - 40 for _, y in blue), "behind: the bottom edge"


# --- fake provider modes ---------------------------------------------------------------------


async def test_vision_dock_fake_provider_dock_and_decline_modes() -> None:
    context = build_context(
        VisionTask.RECOVER,
        state=_state(),
        state_age_seconds=0,
        mower_map=GARDEN,
        commands=[],
        now=T0,
        last_drive=None,
        previous_rounds=[],
        motion=MOTION,
        images=0,
    )
    dock = await FakeVisionProvider("fake/dock").analyse([], context, VisionTask.RECOVER)
    assert dock.result.action is ProposalAction.RETURN_TO_DOCK and dock.result.proposal == []
    declined = await FakeVisionProvider("fake/decline").analyse([], context, VisionTask.FIND_DOCK)
    assert declined.result.action is ProposalAction.NONE
    assert declined.result.situation is Situation.TRAPPED
    decision = decide(declined.result, min_confidence=0.6, dock=context.dock_route)
    assert decision.fallback and decision.action is ProposalAction.RETURN_TO_DOCK
    island = context.model_copy(update={"dock_route": _route(_state(x=45), 45, 5)})
    drive = await FakeVisionProvider("fake/dock").analyse([], island, VisionTask.RECOVER)
    assert drive.result.action is ProposalAction.DRIVE and drive.result.proposal


# --- through the API -------------------------------------------------------------------------


def _created_events(h: Any) -> list[dict[str, Any]]:
    subject = event_subject_for_type(VISION_PROPOSAL_CREATED)
    return [
        EventEnvelope[dict[str, Any]].model_validate_json(m.data).payload
        for m in h.bus.published
        if m.subject == subject
    ]


async def test_vision_dock_fallback_is_offered_approved_and_sent_as_dock_command() -> None:
    declined = answer([], situation=Situation.AWAY_FROM_DOCK, confidence=0.7)
    async with harness(factory=StaticFactory(default=declined)) as h:
        owner = h.owner()
        rig = await h.rig(owner)  # idle on the front lawn, the dock's area
        created = (
            await h.client.post(
                f"{API}/mowers/{rig.mower_id}/recover",
                json={"task": "find_dock"},
                headers=owner.headers,
            )
        ).json()
        body = await h.until(created["id"], owner, ProposalState.PROPOSED)
        assert body["action"] == "return_to_dock" and body["fallback"] is True
        assert body["steps"] == []
        assert body["assessment"]["action"] == "none", "the model's own answer is kept"
        assert body["detail"].startswith(FALLBACK_PREFIX)
        [event] = _created_events(h)
        assert (event["action"], event["fallback"], event["steps"]) == (
            "return_to_dock",
            True,
            0,
        )
        context = h.factory.calls[-1][2]
        assert context.dock_route is not None and context.dock_route.mower_area == "Front lawn"
        assert h.fleet.issued == [], "nothing runs before the approval"
        approved = await h.client.post(
            f"{API}/proposals/{created['id']}/approve", headers=owner.headers
        )
        assert approved.status_code == 200, approved.text
        done = await h.until(created["id"], owner, ProposalState.RECOVERED)
        assert done["detail"].startswith("returning to the dock")
        assert done["return_command_id"] is not None
        [issued] = h.fleet.issued
        assert isinstance(issued.command.command, ReturnToDock)
        assert issued.on_behalf_of.subject == owner.principal.subject
        assert h.fleet.starts == [], "no drive session: the mower drives home by itself"
        assert not [c for c in rig.commands if isinstance(c.command, ManualDrive)]


async def test_vision_dock_lifted_mower_gets_nothing_and_nothing_runs() -> None:
    chosen = answer([], situation=Situation.AWAY_FROM_DOCK, action=ProposalAction.RETURN_TO_DOCK)
    async with harness(factory=StaticFactory(default=chosen)) as h:
        owner = h.owner()
        rig = await h.rig(owner)
        rig.sim.lift()
        await wait_for(
            lambda: (
                (o := h.service.view.state(rig.mower_id)) is not None
                and o.state.raw.get("lifted") is True
            )
        )
        created = (
            await h.client.post(
                f"{API}/mowers/{rig.mower_id}/recover", json={}, headers=owner.headers
            )
        ).json()
        body = await h.until(created["id"], owner, ProposalState.REJECTED_BY_AI)
        assert body["fallback"] is False
        assert "return to dock is not applicable" in body["detail"]
        assert "lifted" in body["detail"]
        response = await h.client.post(
            f"{API}/proposals/{created['id']}/approve", headers=owner.headers
        )
        assert response.status_code == 409
        await asyncio.sleep(0.2)
        assert h.fleet.issued == [] and h.fleet.starts == []
        assert _created_events(h) == []


async def test_vision_dock_lift_after_the_proposal_aborts_before_any_command() -> None:
    chosen = answer([], situation=Situation.AWAY_FROM_DOCK, action=ProposalAction.RETURN_TO_DOCK)
    async with harness(factory=StaticFactory(default=chosen)) as h:
        owner = h.owner()
        rig = await h.rig(owner)
        created = (
            await h.client.post(
                f"{API}/mowers/{rig.mower_id}/recover", json={}, headers=owner.headers
            )
        ).json()
        body = await h.until(created["id"], owner, ProposalState.PROPOSED)
        assert body["action"] == "return_to_dock" and body["fallback"] is False
        rig.sim.lift()
        await wait_for(
            lambda: (
                (o := h.service.view.state(rig.mower_id)) is not None
                and o.state.raw.get("lifted") is True
            )
        )
        await h.client.post(f"{API}/proposals/{created['id']}/approve", headers=owner.headers)
        done = await h.until(created["id"], owner, ProposalState.ABORTED)
        assert "not applicable any more" in done["detail"] and "lifted" in done["detail"]
        assert h.fleet.issued == [] and h.fleet.starts == []


async def test_vision_dock_map_image_is_sent_after_the_frames_and_kept() -> None:
    async with harness(factory=StaticFactory(), vision_map_image=True) as h:
        owner = h.owner()
        rig = await h.rig(owner)
        created = (
            await h.client.post(
                f"{API}/mowers/{rig.mower_id}/recover", json={}, headers=owner.headers
            )
        ).json()
        body = await h.until(created["id"], owner, ProposalState.PROPOSED)
        assert body["image_count"] == 2, "the gateway's photo plus the map"
        assert [f["source"] for f in body["frames"]] == ["mower", "map"]
        _, images, context, _ = h.factory.calls[-1]
        assert images == 2 and context.map_image is True and context.images == 2
        response = await h.client.get(
            f"{API}/proposals/{created['id']}/images/1", headers=owner.headers
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        _decode(response.content)
