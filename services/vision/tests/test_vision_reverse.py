"""AI assist on a drive link without reverse (the Mammotion cloud, ADR 0049): the context says
so, a plan with a backwards step is not offered, and one approved before the link changed is
refused before anything moves (never driven with the step dropped)."""

import base64
from typing import Any

from mowdeck_contracts.v1 import (
    ManualDrive,
    MowerActivity,
    MowerCapabilitiesSnapshot,
    VisionTask,
    utc_now,
)
from mowdeck_contracts.v1.subjects import device_subject
from mowdeck_control import flat_png
from vision_support import API, answer, harness, wait_for

from vision.adapters.providers.fake import FORWARD_RECOVER_STEPS, FakeVisionProvider
from vision.domain.context import Motion, SceneContext
from vision.domain.models import AssessmentOut, ProposalAction, ProposalState, ProposedStep
from vision.domain.rules import decide, evaluate, reverse_problem

BACK = ProposedStep(linear=-0.5, angular=0.0, seconds=1.0)
TURN = ProposedStep(linear=0.0, angular=0.5, seconds=1.0)
AHEAD = ProposedStep(linear=0.5, angular=0.0, seconds=1.0)


def _png_b64() -> str:
    return base64.b64encode(flat_png(8, 6, (1, 2, 3))).decode()


def test_vision_reverse_problem_names_the_backwards_step() -> None:
    assert reverse_problem([BACK, TURN], reverse=True) is None
    assert reverse_problem([TURN, AHEAD], reverse=False) is None
    problem = reverse_problem([TURN, BACK], reverse=False)
    assert problem is not None and problem.startswith("step 2 drives backwards")
    assert "cannot reverse" in problem


def test_vision_reverse_evaluate_and_decide_refuse_backwards_plans() -> None:
    plan = answer([BACK, TURN, AHEAD])
    assert evaluate(plan, min_confidence=0.6) == (ProposalState.PROPOSED, None)
    state, detail = evaluate(plan, min_confidence=0.6, reverse=False)
    assert state is ProposalState.REJECTED_BY_AI and "drives backwards" in (detail or "")
    forward = answer([TURN, AHEAD])
    assert evaluate(forward, min_confidence=0.6, reverse=False)[0] is ProposalState.PROPOSED
    decision = decide(plan, min_confidence=0.6, dock=None, reverse=False)
    assert decision.state is ProposalState.REJECTED_BY_AI
    low = answer([BACK, TURN], confidence=0.3)
    refused = decide(low, min_confidence=0.6, dock=None, reverse=False, path_check=lambda _: None)
    assert refused.state is ProposalState.REJECTED_BY_AI
    assert "drives backwards" in (refused.model_refused or "")


async def test_vision_reverse_fake_provider_plans_forwards_without_reverse() -> None:
    motion = Motion(linear_mps_at_1=0.5, turn_deg_per_s_at_1=57.0)
    assert motion.reverse is True
    cloud = SceneContext(
        task=VisionTask.RECOVER, motion=motion.model_copy(update={"reverse": False})
    )
    recover = await FakeVisionProvider().analyse([], cloud, VisionTask.RECOVER)
    assert recover.result.proposal == list(FORWARD_RECOVER_STEPS)
    assert all(step.linear >= 0 for step in recover.result.proposal)
    bluetooth = SceneContext(task=VisionTask.RECOVER, motion=motion)
    back = await FakeVisionProvider().analyse([], bluetooth, VisionTask.RECOVER)
    assert back.result.proposal[0].linear < 0


async def test_vision_reverse_flow_without_reverse_offers_no_backwards_plan() -> None:
    async with harness() as h:
        owner = h.owner()
        rig = await h.rig(owner, drive_reverse=False)
        rig.sim.inject_stuck()
        await wait_for(
            lambda: (
                (o := h.service.view.state(rig.mower_id)) is not None
                and o.state.activity is MowerActivity.ERROR
            )
        )
        await wait_for(lambda: h.service.view.capabilities(rig.mower_id) is not None)
        h.factory.default = answer([BACK, TURN])
        response = await h.client.post(
            f"{API}/mowers/{rig.mower_id}/recover",
            json={"task": "recover", "images": [{"media_type": "image/png", "data": _png_b64()}]},
            headers=owner.headers,
        )
        assert response.status_code == 202, response.text
        created = response.json()
        done = await h.until(
            created["id"], owner, ProposalState.REJECTED_BY_AI, ProposalState.PROPOSED
        )
        _, _, context, task = h.factory.calls[0]
        assert task is VisionTask.RECOVER and context.motion.reverse is False
        if done["state"] == "proposed":  # the dock fallback, never the backwards plan
            assert done["action"] == ProposalAction.RETURN_TO_DOCK.value
        assert "drives backwards" in (done["detail"] or "")
        assert not any(isinstance(c.command, ManualDrive) for c in rig.commands)


async def test_vision_reverse_executor_refuses_when_the_link_lost_reverse() -> None:
    async with harness() as h:
        owner = h.owner()
        rig = await h.rig(owner)  # reverse available while the plan is made
        rig.sim.inject_stuck()
        await wait_for(
            lambda: (
                (o := h.service.view.state(rig.mower_id)) is not None
                and o.state.activity is MowerActivity.ERROR
            )
        )
        h.factory.default = answer([TURN, BACK])
        response = await h.client.post(
            f"{API}/mowers/{rig.mower_id}/recover",
            json={"task": "recover", "images": [{"media_type": "image/png", "data": _png_b64()}]},
            headers=owner.headers,
        )
        created = response.json()
        await h.until(created["id"], owner, ProposalState.PROPOSED)
        # The bridge went down: the drive link is the cloud now (no reverse).
        caps = (await rig.sim.capabilities()).model_copy(update={"drive_reverse": False})
        await h.bus.publish(
            device_subject(rig.mower_id, "capabilities"),
            MowerCapabilitiesSnapshot(
                mower_id=rig.mower_id,
                account_id=rig.account_id,
                observed_at=utc_now(),
                capabilities=caps,
            ),
        )
        await wait_for(
            lambda: (
                (c := h.service.view.capabilities(rig.mower_id)) is not None
                and not c[1].drive_reverse
            )
        )
        approved = await h.client.post(
            f"{API}/proposals/{created['id']}/approve", headers=owner.headers
        )
        assert approved.status_code == 200, approved.text
        done: dict[str, Any] = await h.until(created["id"], owner, ProposalState.ABORTED)
        assert "step 2 drives backwards" in done["detail"]
        assert "nothing was driven" in done["detail"]
        assert not any(isinstance(c.command, ManualDrive) for c in rig.commands)
        assert h.fleet.starts == []  # no drive session was opened


def test_vision_reverse_answer_model_accepts_forward_only_steps() -> None:
    out = AssessmentOut.model_validate(answer(list(FORWARD_RECOVER_STEPS)).model_dump())
    assert out.proposal == list(FORWARD_RECOVER_STEPS)
