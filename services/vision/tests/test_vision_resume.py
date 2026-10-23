"""``resume`` end to end on the in-memory bus with a simulated mower: a multi-area job, the
mower stuck at the entry of the transfer path to the next area, the job in the context, the
proposal, and after approval drive, verify, ``resume_mow`` in that order; refusals when no job
is paused, when the drive did not help and when Fleet or the mower refuse."""

import math
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from mowdeck_contracts.v1 import (
    VISION_PROPOSAL_CREATED,
    CommandEnvelope,
    EventEnvelope,
    IssueCommandErrorCode,
    ManualDrive,
    MowerActivity,
    PauseMow,
    ReturnToDock,
    StartMow,
    VisionTask,
)
from mowdeck_contracts.v1.subjects import event_subject_for_type
from mowdeck_service_kit.bus import publish_command
from vision_support import (
    API,
    Harness,
    Person,
    SimRig,
    StaticFactory,
    answer,
    harness,
    wait_for,
)

from vision.adapters.providers.fake import FakeVisionProvider
from vision.application.ports import VisionProvider
from vision.domain.context import SceneContext
from vision.domain.models import (
    Assessment,
    Image,
    ProposalAction,
    ProposalState,
    ProposedStep,
    ProviderConfig,
)

DONE = (
    ProposalState.RECOVERED,
    ProposalState.NOT_RECOVERED,
    ProposalState.ABORTED,
    ProposalState.FAILED,
)


class _RecordingFake:
    """The rule based fake provider, recording what it was asked like ``StaticProvider``."""

    def __init__(self, config: ProviderConfig, factory: "FakeFactory") -> None:
        self.config = config
        self.factory = factory

    async def analyse(
        self, images: list[Image], context: SceneContext, task: VisionTask
    ) -> Assessment:
        self.factory.calls.append((self.config, len(images), context, task))
        return await FakeVisionProvider(self.factory.model).analyse(images, context, task)


@dataclass
class FakeFactory(StaticFactory):
    """Every model is the fake provider ``model`` (default ``fake/resume``)."""

    model: str = "fake/resume"

    def create(self, config: ProviderConfig) -> VisionProvider:
        return _RecordingFake(config, self)


async def _command(h: Harness, rig: SimRig, owner: Person, command: Any) -> None:
    """A job command like Fleet publishes it (the view sees it, the simulated gateway runs it)."""
    await publish_command(
        h.bus,
        CommandEnvelope.create(
            mower_id=rig.mower_id,
            account_id=owner.account_id,
            issued_by=owner.principal.ref(),
            command=command,
            ttl=timedelta(seconds=30),
        ),
    )


async def _mowing_two_areas(h: Harness, owner: Person) -> SimRig:
    """A mower mowing ``front`` then ``back`` (the simulator's garden: the side path from
    (1, 8) to (1, 11) links them)."""
    rig = await h.rig(owner)
    await _command(h, rig, owner, StartMow(area_ids=["front", "back"]))
    await wait_for(lambda: rig.sim.snapshot().activity is MowerActivity.MOWING)
    await wait_for(
        lambda: (
            (path := h.service.view.mow_path(rig.mower_id)) is not None
            and path[1] is not None
            and path[1].area_ids == ["front", "back"]
        )
    )
    return rig


async def _stuck_at_the_side_path(h: Harness, rig: SimRig) -> None:
    """The far end of ``front``, 0.8 m before the side path, facing it; then stuck (1201)."""
    rig.sim._x, rig.sim._y, rig.sim._theta = 1.0, 7.2, math.pi / 2
    rig.sim.inject_stuck("The robot is stuck")
    await wait_for(
        lambda: (
            (o := h.service.view.state(rig.mower_id)) is not None
            and o.state.activity is MowerActivity.ERROR
        )
    )


async def _request(h: Harness, owner: Person, rig: SimRig) -> str:
    response = await h.client.post(
        f"{API}/mowers/{rig.mower_id}/recover", json={"task": "recover"}, headers=owner.headers
    )
    assert response.status_code == 202, response.text
    proposal_id: str = response.json()["id"]
    return proposal_id


def _kinds(rig: SimRig) -> list[str]:
    return [c.command.kind for c in rig.commands]


async def test_vision_resume_at_the_channel_entry_end_to_end() -> None:
    factory = FakeFactory()
    async with harness(factory=factory) as h:
        owner = h.owner()
        rig = await _mowing_two_areas(h, owner)
        await _stuck_at_the_side_path(h, rig)
        proposal_id = await _request(h, owner, rig)
        proposal = await h.until(proposal_id, owner, ProposalState.PROPOSED, *DONE)
        assert proposal["state"] == "proposed", proposal["detail"]
        # The model saw the job: front being mowed, back next, the side path right ahead.
        _, _, context, _ = factory.calls[0]
        job = context.job
        assert job is not None and job.status == "paused_by_error" and job.resumable
        assert (job.current_area, job.next_area) == ("Front lawn", "Back lawn")
        link = job.connection_to_next
        assert link is not None and link.path_name == "Side path" and link.mower_at_entry
        assert link.entry_distance_m < 1.0 and abs(link.entry_bearing_deg) < 15
        assert context.dock_route is not None and context.dock_route.return_to_dock_applicable
        # The proposal: resume after a short drive, with the job for the app.
        assert proposal["action"] == "resume" and proposal["fallback"] is False
        assert proposal["assessment"]["action"] == "resume"
        assert [s["linear"] for s in proposal["steps"]] == [-0.8]
        assert proposal["job"]["current_area"] == "Front lawn"
        assert proposal["job"]["next_area"] == "Back lawn"
        assert proposal["job"]["mower_at_entry"] is True
        assert [a["status"] for a in proposal["job"]["areas"]] == ["current", "next"]
        events = [
            EventEnvelope[dict[str, Any]].model_validate_json(m.data)
            for m in h.bus.published
            if m.subject == event_subject_for_type(VISION_PROPOSAL_CREATED)
        ]
        assert [e.payload["action"] for e in events] == ["resume"]
        assert events[0].payload["steps"] == 1
        # Nothing ran before the approval.
        assert "manual_drive" not in _kinds(rig) and "resume_mow" not in _kinds(rig)
        approved = await h.client.post(
            f"{API}/proposals/{proposal_id}/approve", headers=owner.headers
        )
        assert approved.status_code == 200, approved.text
        done = await h.until(proposal_id, owner, *DONE, timeout=20)
        assert done["state"] == "recovered", done["detail"]
        assert "free again" in done["detail"]
        assert "the job continues (Front lawn, next Back lawn)" in done["detail"]
        # Order: drive session, frames (blades off), session closed, then resume_mow.
        [start] = h.fleet.starts
        assert start.blade_on_allowed is False and start.origin == "vision"
        assert len(h.fleet.stops) == 1
        kinds = _kinds(rig)
        last_frame = max(i for i, k in enumerate(kinds) if k == "manual_drive")
        assert kinds.index("resume_mow") > last_frame
        frames = [c.command for c in rig.commands if isinstance(c.command, ManualDrive)]
        assert frames and all(not f.blade_on for f in frames)
        [issued] = h.fleet.issued
        assert issued.command.command.kind == "resume_mow" and issued.origin == "vision"
        assert issued.on_behalf_of.subject == owner.principal.subject
        assert done["return_command_id"] is not None
        await wait_for(lambda: rig.sim.snapshot().activity is MowerActivity.MOWING)


async def test_vision_resume_refused_when_no_job_is_paused() -> None:
    factory = StaticFactory(default=answer([], action=ProposalAction.RESUME))
    async with harness(factory=factory) as h:
        owner = h.owner()
        rig = await h.rig(owner)  # idle on the front lawn, no job
        rig.sim.inject_stuck()
        await wait_for(
            lambda: (
                (o := h.service.view.state(rig.mower_id)) is not None
                and o.state.activity is MowerActivity.ERROR
            )
        )
        proposal_id = await _request(h, owner, rig)
        proposal = await h.until(proposal_id, owner, ProposalState.PROPOSED, *DONE)
        # Not applicable: the labelled dock fallback is offered instead, never a resume.
        assert proposal["action"] == "return_to_dock" and proposal["fallback"] is True
        assert "resume is not applicable: no mowing job" in proposal["detail"]
        assert proposal["job"] is None


async def test_vision_resume_refused_at_execution_when_the_job_ended() -> None:
    factory = StaticFactory(default=answer([], action=ProposalAction.RESUME))
    async with harness(factory=factory) as h:
        owner = h.owner()
        rig = await _mowing_two_areas(h, owner)
        await _command(h, rig, owner, PauseMow())
        await wait_for(
            lambda: (
                (o := h.service.view.state(rig.mower_id)) is not None
                and o.state.activity is MowerActivity.PAUSED
            )
        )
        proposal_id = await _request(h, owner, rig)
        proposal = await h.until(proposal_id, owner, ProposalState.PROPOSED, *DONE)
        assert proposal["action"] == "resume" and proposal["steps"] == []
        assert proposal["job"]["status"] == "paused"
        # Someone sends the mower home before the approval.
        await _command(h, rig, owner, ReturnToDock(end_job=True))
        await wait_for(
            lambda: (
                (o := h.service.view.state(rig.mower_id)) is not None
                and o.state.activity is not MowerActivity.PAUSED
            )
        )
        await h.client.post(f"{API}/proposals/{proposal_id}/approve", headers=owner.headers)
        done = await h.until(proposal_id, owner, *DONE)
        assert done["state"] == "aborted"
        assert done["detail"].startswith("resume refused:")
        assert h.fleet.issued == [] and h.fleet.starts == []


async def test_vision_resume_without_steps_continues_a_paused_job() -> None:
    factory = StaticFactory(default=answer([], action=ProposalAction.RESUME))
    async with harness(factory=factory) as h:
        owner = h.owner()
        rig = await _mowing_two_areas(h, owner)
        await _command(h, rig, owner, PauseMow())
        await wait_for(lambda: rig.sim.snapshot().activity is MowerActivity.PAUSED)
        proposal_id = await _request(h, owner, rig)
        await h.until(proposal_id, owner, ProposalState.PROPOSED)
        await h.client.post(f"{API}/proposals/{proposal_id}/approve", headers=owner.headers)
        done = await h.until(proposal_id, owner, *DONE)
        assert done["state"] == "recovered", done["detail"]
        assert done["detail"].startswith("the job continues")
        assert h.fleet.starts == [], "no drive session without steps"
        assert [i.command.command.kind for i in h.fleet.issued] == ["resume_mow"]
        await wait_for(lambda: rig.sim.snapshot().activity is MowerActivity.MOWING)


async def test_vision_resume_not_sent_when_the_drive_did_not_free_the_mower() -> None:
    forward = [ProposedStep(linear=0.5, angular=0.0, seconds=0.6)]  # a stuck mower ignores it
    factory = StaticFactory(default=answer(forward, action=ProposalAction.RESUME))
    async with harness(factory=factory) as h:
        owner = h.owner()
        rig = await _mowing_two_areas(h, owner)
        await _stuck_at_the_side_path(h, rig)
        proposal_id = await _request(h, owner, rig)
        await h.until(proposal_id, owner, ProposalState.PROPOSED)
        await h.client.post(f"{API}/proposals/{proposal_id}/approve", headers=owner.headers)
        done = await h.until(proposal_id, owner, *DONE)
        assert done["state"] == "not_recovered"
        assert done["detail"].endswith("the job was not resumed")
        assert h.fleet.issued == [], "resume_mow is never sent after a failed drive"
        assert "resume_mow" not in _kinds(rig)
        assert rig.sim.stuck


async def test_vision_resume_reports_a_refusal_by_fleet() -> None:
    factory = StaticFactory(default=answer([], action=ProposalAction.RESUME))
    async with harness(factory=factory) as h:
        owner = h.owner()
        rig = await _mowing_two_areas(h, owner)
        await _command(h, rig, owner, PauseMow())
        await wait_for(lambda: rig.sim.snapshot().activity is MowerActivity.PAUSED)
        proposal_id = await _request(h, owner, rig)
        await h.until(proposal_id, owner, ProposalState.PROPOSED)
        h.fleet.refuse_issue = IssueCommandErrorCode.FORBIDDEN
        await h.client.post(f"{API}/proposals/{proposal_id}/approve", headers=owner.headers)
        done = await h.until(proposal_id, owner, *DONE)
        assert done["state"] == "not_recovered"
        assert done["detail"] == "resume_mow was refused: forbidden"


async def test_vision_resume_sent_once_and_reported_when_the_job_does_not_continue() -> None:
    # Resume without a maneuver while the mower is still wedged: the error persists, the gate
    # lets resume_mow go out (the error itself is no reason), the wheels spin, nothing moves.
    factory = StaticFactory(default=answer([], action=ProposalAction.RESUME))
    async with harness(factory=factory, vision_resume_confirm_seconds=1.5) as h:
        owner = h.owner()
        rig = await _mowing_two_areas(h, owner)
        await _stuck_at_the_side_path(h, rig)
        proposal_id = await _request(h, owner, rig)
        await h.until(proposal_id, owner, ProposalState.PROPOSED)
        await h.client.post(f"{API}/proposals/{proposal_id}/approve", headers=owner.headers)
        done = await h.until(proposal_id, owner, *DONE)
        assert done["state"] == "not_recovered", done["detail"]
        assert "resume_mow was sent once" in done["detail"]
        assert "still reports stuck, 0.0 m from where resume_mow was sent" in done["detail"]
        assert [i.command.command.kind for i in h.fleet.issued] == ["resume_mow"]
        assert _kinds(rig).count("resume_mow") == 1, "never sent a second time"
        assert rig.sim.stuck


async def test_vision_resume_after_the_drive_keeps_the_error_until_the_job_moves() -> None:
    factory = FakeFactory()
    async with harness(factory=factory) as h:
        owner = h.owner()
        rig = await _mowing_two_areas(h, owner)
        await _stuck_at_the_side_path(h, rig)
        proposal_id = await _request(h, owner, rig)
        await h.until(proposal_id, owner, ProposalState.PROPOSED)
        await h.client.post(f"{API}/proposals/{proposal_id}/approve", headers=owner.headers)
        await wait_for(lambda: "resume_mow" in _kinds(rig), timeout=15)
        # resume_mow went out while the mower still reported the stuck error.
        resumed = _kinds(rig).index("resume_mow")
        assert resumed > 0
        done = await h.until(proposal_id, owner, *DONE, timeout=20)
        assert done["state"] == "recovered", done["detail"]
        assert "still reports stuck" in done["detail"], "the drive did not clear the error"
        assert "the job continues" in done["detail"]
