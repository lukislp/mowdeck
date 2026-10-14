"""AI assist end to end on the in-memory bus with a simulated mower: request, proposal, event,
approval, execution through the drive channel, verification, aborts, rejection, retry."""

import asyncio
import base64
from typing import Any
from uuid import uuid4

from mowdeck_contracts.v1 import (
    VISION_PROPOSAL_CREATED,
    DriveEndReason,
    DriveErrorCode,
    EventEnvelope,
    ManualDrive,
    MowerActivity,
    VisionTask,
)
from mowdeck_contracts.v1.subjects import command_subject, event_subject_for_type
from mowdeck_control import flat_png
from mowdeck_service_kit.testing import FakeEntitlements
from vision_support import (
    API,
    StaticFactory,
    answer,
    harness,
    wait_for,
)

from vision.domain.models import ProposalState, ProposedStep, RiskFlag, Situation

RECOVERED = ProposalState.RECOVERED


def _png_b64() -> str:
    return base64.b64encode(flat_png(8, 6, (1, 2, 3))).decode()


async def test_vision_flow_recover_stuck_mower_end_to_end() -> None:
    async with harness() as h:
        owner = h.owner()
        rig = await h.rig(owner)
        rig.sim.inject_stuck()
        await wait_for(
            lambda: (
                (o := h.service.view.state(rig.mower_id)) is not None
                and o.state.activity is MowerActivity.ERROR
            )
        )
        response = await h.client.post(
            f"{API}/mowers/{rig.mower_id}/recover",
            json={"task": "recover", "images": [{"media_type": "image/png", "data": _png_b64()}]},
            headers=owner.headers,
        )
        assert response.status_code == 202, response.text
        created = response.json()
        assert created["state"] == "analysing"
        assert created["hosted"] is True and created["provider_model"] == "fake/recover"
        proposal = await h.until(created["id"], owner, ProposalState.PROPOSED)
        assert proposal["assessment"]["situation"] == "stuck_on_obstacle"
        assert [s["linear"] for s in proposal["steps"]] == [-1.0, 0.0]
        assert proposal["expires_at"] is not None
        _, images, context, task = h.factory.calls[0]
        assert images == 1 and task is VisionTask.RECOVER
        assert context.mower is not None and context.mower.error_code == "stuck"
        assert context.mower.drive_recoverable is True
        assert context.map is not None and context.map.dock is not None
        # The event Notifications turns into vision_proposal.
        events = [
            m
            for m in h.bus.published
            if m.subject == event_subject_for_type(VISION_PROPOSAL_CREATED)
        ]
        [event] = [EventEnvelope[dict[str, Any]].model_validate_json(m.data) for m in events]
        assert event.account_id == owner.account_id
        assert event.payload["link"] == f"/mowers/{rig.mower_id}?assist={created['id']}"
        # Approve: the steps are driven through a drive session on the owner's behalf.
        approved = await h.client.post(
            f"{API}/proposals/{created['id']}/approve", headers=owner.headers
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["state"] == "executing"
        again = await h.client.post(
            f"{API}/proposals/{created['id']}/approve", headers=owner.headers
        )
        assert again.status_code == 409, "one approval per proposal"
        done = await h.until(created["id"], owner, RECOVERED, ProposalState.NOT_RECOVERED)
        assert done["state"] == "recovered", done["detail"]
        assert done["approved_by"]["subject"] == str(owner.principal.subject)
        assert [s["outcome"] for s in done["steps"]] == ["done", "done"]
        assert all(s["frames"] >= 4 for s in done["steps"])
        [start] = h.fleet.starts
        assert start.origin == "vision"
        assert start.on_behalf_of.subject == owner.principal.subject
        assert start.blade_on_allowed is False
        [stop] = h.fleet.stops
        assert stop.reason is DriveEndReason.COMPLETED
        assert stop.session_id == __import__("uuid").UUID(done["drive_session_id"])
        frames = [c.command for c in rig.commands if isinstance(c.command, ManualDrive)]
        assert frames and all(not f.blade_on for f in frames)
        assert all(f.ttl_ms == 1000 for f in frames)
        assert frames[-1].linear == 0 and frames[-1].angular == 0
        assert not rig.sim.stuck
        state = await rig.sim.get_state()
        assert state.activity in (MowerActivity.IDLE, MowerActivity.MOWING)
        # Cost accounting of the analysis.
        [request] = h.store.requests
        assert (request.input_tokens, request.output_tokens) == (1200, 150)
        assert request.hosted is True and request.kind == "analysis"


async def test_vision_flow_risk_flag_is_rejected_by_ai_and_never_executes() -> None:
    factory = StaticFactory(default=answer(risk=[RiskFlag.PEOPLE_VISIBLE]))
    async with harness(factory=factory) as h:
        owner = h.owner()
        rig = await h.rig(owner)
        created = (
            await h.client.post(
                f"{API}/mowers/{rig.mower_id}/recover", json={}, headers=owner.headers
            )
        ).json()
        rejected = await h.until(created["id"], owner, ProposalState.REJECTED_BY_AI)
        assert rejected["detail"] == "risk: people_visible"
        assert rejected["assessment"]["risk"] == ["people_visible"]
        response = await h.client.post(
            f"{API}/proposals/{created['id']}/approve", headers=owner.headers
        )
        assert response.status_code == 409
        await asyncio.sleep(0.2)
        assert h.fleet.starts == []
        assert not [c for c in rig.commands if isinstance(c.command, ManualDrive)]
        assert not [
            m
            for m in h.bus.published
            if m.subject == event_subject_for_type(VISION_PROPOSAL_CREATED)
        ]
        assert rejected["can_retry"] is True


async def test_vision_flow_low_confidence_and_empty_answers_are_rejected() -> None:
    factory = StaticFactory(
        default=answer(confidence=0.3),
        answers={VisionTask.FIND_DOCK: answer(steps=[], situation=Situation.UNKNOWN)},
    )
    async with harness(factory=factory) as h:
        owner = h.owner()
        rig = await h.rig(owner)
        low = (
            await h.client.post(
                f"{API}/mowers/{rig.mower_id}/recover", json={}, headers=owner.headers
            )
        ).json()
        body = await h.until(low["id"], owner, ProposalState.REJECTED_BY_AI)
        assert body["detail"].startswith("confidence 0.30")
        empty = (
            await h.client.post(
                f"{API}/mowers/{rig.mower_id}/recover",
                json={"task": "find_dock"},
                headers=owner.headers,
            )
        ).json()
        body = await h.until(empty["id"], owner, ProposalState.REJECTED_BY_AI)
        assert "no safe maneuver" in body["detail"]


async def test_vision_flow_emergency_stop_aborts_the_execution() -> None:
    slow = [ProposedStep(linear=0.0, angular=0.3, seconds=3.0)] * 3
    async with harness(factory=StaticFactory(default=answer(slow))) as h:
        owner = h.owner()
        rig = await h.rig(owner)
        created = (
            await h.client.post(
                f"{API}/mowers/{rig.mower_id}/recover", json={}, headers=owner.headers
            )
        ).json()
        await h.until(created["id"], owner, ProposalState.PROPOSED)
        await h.client.post(f"{API}/proposals/{created['id']}/approve", headers=owner.headers)
        await wait_for(lambda: any(isinstance(c.command, ManualDrive) for c in rig.commands))
        frames_before = len(rig.commands)
        await rig.emergency_stop(owner.principal)
        done = await h.until(created["id"], owner, ProposalState.ABORTED)
        assert done["detail"].startswith("emergency_stop")
        assert [s["outcome"] for s in done["steps"]] == ["aborted", "skipped", "skipped"]
        [stop] = h.fleet.stops
        assert stop.reason is DriveEndReason.ABORTED
        await asyncio.sleep(0.3)
        later = [c for c in rig.commands[frames_before:] if isinstance(c.command, ManualDrive)]
        assert later == [], "no frame (not even a zero frame) after an emergency stop"


async def test_vision_flow_sensor_stop_and_cancel_abort() -> None:
    slow = [ProposedStep(linear=0.0, angular=0.3, seconds=3.0)] * 2
    async with harness(factory=StaticFactory(default=answer(slow))) as h:
        owner = h.owner()
        rig = await h.rig(owner)
        first = (
            await h.client.post(
                f"{API}/mowers/{rig.mower_id}/recover", json={}, headers=owner.headers
            )
        ).json()
        await h.until(first["id"], owner, ProposalState.PROPOSED)
        await h.client.post(f"{API}/proposals/{first['id']}/approve", headers=owner.headers)
        await wait_for(lambda: any(isinstance(c.command, ManualDrive) for c in rig.commands))
        rig.sim.lift()
        done = await h.until(first["id"], owner, ProposalState.ABORTED)
        assert done["detail"].startswith("sensor_stop")
        rig.sim.clear_error()
        second = (
            await h.client.post(
                f"{API}/mowers/{rig.mower_id}/recover", json={}, headers=owner.headers
            )
        ).json()
        await h.until(second["id"], owner, ProposalState.PROPOSED)
        await h.client.post(f"{API}/proposals/{second['id']}/approve", headers=owner.headers)
        await wait_for(lambda: len(h.fleet.starts) == 2)
        cancel = await h.client.post(
            f"{API}/proposals/{second['id']}/reject", headers=owner.headers
        )
        assert cancel.status_code == 200
        done = await h.until(second["id"], owner, ProposalState.ABORTED)
        assert done["detail"].startswith("cancelled")
        assert done["decided_by"]["subject"] == str(owner.principal.subject)
        last = rig.commands[-1].command
        assert isinstance(last, ManualDrive) and (last.linear, last.angular) == (0, 0)


async def test_vision_flow_no_state_aborts_and_fleet_refusal_is_reported() -> None:
    async with harness(vision_start_state_max_age_seconds=2.0) as h:
        owner = h.owner()
        rig = await h.rig(owner)
        created = (
            await h.client.post(
                f"{API}/mowers/{rig.mower_id}/recover", json={}, headers=owner.headers
            )
        ).json()
        await h.until(created["id"], owner, ProposalState.PROPOSED)
        h.fleet.refuse = DriveErrorCode.BUSY
        await h.client.post(f"{API}/proposals/{created['id']}/approve", headers=owner.headers)
        done = await h.until(created["id"], owner, ProposalState.ABORTED)
        assert done["detail"].startswith("drive refused: busy")
        h.fleet.refuse = None
        rig.publish_states = False
        await asyncio.sleep(2.2)  # older than the state timeout
        second = (
            await h.client.post(
                f"{API}/mowers/{rig.mower_id}/recover", json={}, headers=owner.headers
            )
        ).json()
        await h.until(second["id"], owner, ProposalState.PROPOSED)
        await h.client.post(f"{API}/proposals/{second['id']}/approve", headers=owner.headers)
        done = await h.until(second["id"], owner, ProposalState.ABORTED)
        assert done["detail"].startswith("no_state")
        assert len(h.fleet.starts) == 1, "no drive session without a fresh state"


async def test_vision_flow_states_stop_during_execution_abort() -> None:
    slow = [ProposedStep(linear=0.0, angular=0.3, seconds=3.0)]
    async with harness(factory=StaticFactory(default=answer(slow))) as h:
        owner = h.owner()
        rig = await h.rig(owner)
        created = (
            await h.client.post(
                f"{API}/mowers/{rig.mower_id}/recover", json={}, headers=owner.headers
            )
        ).json()
        await h.until(created["id"], owner, ProposalState.PROPOSED)
        await h.client.post(f"{API}/proposals/{created['id']}/approve", headers=owner.headers)
        await wait_for(lambda: any(isinstance(c.command, ManualDrive) for c in rig.commands))
        rig.publish_states = False
        done = await h.until(created["id"], owner, ProposalState.ABORTED)
        assert done["detail"].startswith("no_state: no state for")


async def test_vision_flow_find_dock_ends_with_return_to_dock() -> None:
    steps = [ProposedStep(linear=0.8, angular=0.0, seconds=1.2)]
    factory = StaticFactory(
        answers={VisionTask.FIND_DOCK: answer(steps, situation=Situation.AWAY_FROM_DOCK)}
    )
    async with harness(factory=factory) as h:
        owner = h.owner()
        rig = await h.rig(owner)
        created = (
            await h.client.post(
                f"{API}/mowers/{rig.mower_id}/recover",
                json={"task": "find_dock"},
                headers=owner.headers,
            )
        ).json()
        await h.until(created["id"], owner, ProposalState.PROPOSED)
        await h.client.post(f"{API}/proposals/{created['id']}/approve", headers=owner.headers)
        done = await h.until(created["id"], owner, RECOVERED, ProposalState.NOT_RECOVERED)
        assert done["state"] == "recovered", done["detail"]
        assert done["return_command_id"] is not None
        [issued] = h.fleet.issued
        assert issued.origin == "vision" and issued.command.command.kind == "return_to_dock"
        assert issued.on_behalf_of.subject == owner.principal.subject
        await wait_for(
            lambda: (
                rig.sim.snapshot().activity
                in (MowerActivity.RETURNING, MowerActivity.DOCKED, MowerActivity.CHARGING)
            )
        )


async def test_vision_flow_not_recovered_then_retry_with_history_until_the_limit() -> None:
    turn_only = [ProposedStep(linear=0.0, angular=0.5, seconds=0.4)]
    async with harness(factory=StaticFactory(default=answer(turn_only)), vision_max_rounds=2) as h:
        owner = h.owner()
        rig = await h.rig(owner)
        rig.sim.inject_stuck()
        await wait_for(
            lambda: (
                (o := h.service.view.state(rig.mower_id)) is not None
                and o.state.activity is MowerActivity.ERROR
            )
        )
        first = (
            await h.client.post(
                f"{API}/mowers/{rig.mower_id}/recover", json={}, headers=owner.headers
            )
        ).json()
        await h.until(first["id"], owner, ProposalState.PROPOSED)
        await h.client.post(f"{API}/proposals/{first['id']}/approve", headers=owner.headers)
        done = await h.until(first["id"], owner, ProposalState.NOT_RECOVERED, RECOVERED)
        assert done["state"] == "not_recovered"
        assert "still reports stuck" in done["detail"]
        assert done["can_retry"] is True and done["rounds_left"] == 1
        retried = await h.client.post(
            f"{API}/proposals/{first['id']}/retry", json={}, headers=owner.headers
        )
        assert retried.status_code == 202, retried.text
        second = retried.json()
        assert second["round"] == 2 and second["chain_id"] == first["chain_id"]
        await h.until(second["id"], owner, ProposalState.PROPOSED)
        context = h.factory.calls[-1][2]
        assert [r.outcome for r in context.previous_rounds] == ["not_recovered"]
        again = await h.client.post(
            f"{API}/proposals/{first['id']}/retry", json={}, headers=owner.headers
        )
        assert again.status_code == 409, "only the newest round, and one open proposal per mower"
        await h.client.post(f"{API}/proposals/{second['id']}/reject", headers=owner.headers)
        final = await h.proposal(second["id"], owner)
        assert final["state"] == "rejected" and final["can_retry"] is False
        limit = await h.client.post(
            f"{API}/proposals/{second['id']}/retry", json={}, headers=owner.headers
        )
        assert limit.status_code == 409


async def test_vision_flow_reject_expiry_and_one_open_proposal_per_mower() -> None:
    async with harness(vision_proposal_ttl_seconds=30) as h:
        owner = h.owner()
        rig = await h.rig(owner)
        url = f"{API}/mowers/{rig.mower_id}/recover"
        created = (await h.client.post(url, json={}, headers=owner.headers)).json()
        busy = await h.client.post(url, json={}, headers=owner.headers)
        assert busy.status_code == 409
        await h.until(created["id"], owner, ProposalState.PROPOSED)
        rejected = await h.client.post(
            f"{API}/proposals/{created['id']}/reject", headers=owner.headers
        )
        assert rejected.json()["state"] == "rejected"
        assert (
            await h.client.post(f"{API}/proposals/{created['id']}/reject", headers=owner.headers)
        ).status_code == 409
        second = (await h.client.post(url, json={}, headers=owner.headers)).json()
        await h.until(second["id"], owner, ProposalState.PROPOSED)
        stored = h.store.proposals[__import__("uuid").UUID(second["id"])]
        stored.expires_at = stored.created_at  # long gone
        expired = await h.client.post(
            f"{API}/proposals/{second['id']}/approve", headers=owner.headers
        )
        assert expired.status_code == 409
        assert expired.json()["type"] == "urn:mowdeck:problem:vision-proposal-expired"
        assert (await h.proposal(second["id"], owner))["state"] == "expired"
        assert h.fleet.starts == []


async def test_vision_flow_access_rules() -> None:
    async with harness() as h:
        owner = h.owner()
        rig = await h.rig(owner)
        viewer = h.viewer(owner, rig.mower_id)
        stranger = h.owner()
        url = f"{API}/mowers/{rig.mower_id}/recover"
        assert (await h.client.post(url, json={}, headers=viewer.headers)).status_code == 403
        assert (await h.client.post(url, json={}, headers=stranger.headers)).status_code == 404
        assert (await h.client.post(url, json={})).status_code == 401
        created = (await h.client.post(url, json={}, headers=owner.headers)).json()
        await h.until(created["id"], owner, ProposalState.PROPOSED)
        # The viewer may read but not decide; another account sees nothing.
        assert (
            await h.client.get(f"{API}/proposals/{created['id']}", headers=viewer.headers)
        ).status_code == 200
        assert (
            await h.client.post(f"{API}/proposals/{created['id']}/approve", headers=viewer.headers)
        ).status_code == 403
        assert (
            await h.client.get(f"{API}/proposals/{created['id']}", headers=stranger.headers)
        ).status_code == 404
        assert (
            await h.client.post(f"{API}/proposals/{created['id']}/reject", headers=stranger.headers)
        ).status_code == 404
        assert (
            await h.client.get(f"{API}/proposals/{uuid4()}", headers=owner.headers)
        ).status_code == 404
        listed = await h.client.get(
            f"{API}/mowers/{rig.mower_id}/proposals", headers=viewer.headers
        )
        assert [p["id"] for p in listed.json()["items"]] == [created["id"]]
        foreign = await h.client.get(
            f"{API}/mowers/{rig.mower_id}/proposals", headers=stranger.headers
        )
        assert foreign.status_code == 404


async def test_vision_flow_plan_provider_and_quota_rules() -> None:
    plans = FakeEntitlements(plan="home", features=["view", "control"])
    async with harness(entitlements=plans, vision_user_daily_requests=2) as h:
        owner = h.owner()
        rig = await h.rig(owner)
        url = f"{API}/mowers/{rig.mower_id}/recover"
        denied = await h.client.post(url, json={}, headers=owner.headers)
        assert denied.status_code == 403
        assert denied.json()["type"] == "urn:mowdeck:problem:feature-not-in-plan"
        plans.features = ["view", "control", "ai_recover"]  # own provider only
        missing = await h.client.post(url, json={}, headers=owner.headers)
        assert missing.status_code == 409
        assert missing.json()["type"] == "urn:mowdeck:problem:vision-provider-missing"
        plans.features = ["view", "control", "ai_recover", "ai_recover_hosted"]
        for _ in range(2):
            created = (await h.client.post(url, json={}, headers=owner.headers)).json()
            await h.until(created["id"], owner, ProposalState.PROPOSED)
            await h.client.post(f"{API}/proposals/{created['id']}/reject", headers=owner.headers)
        over = await h.client.post(url, json={}, headers=owner.headers)
        assert over.status_code == 429
        assert over.json()["type"] == "urn:mowdeck:problem:vision-quota"


async def test_vision_flow_hosted_account_cap_and_failed_analysis() -> None:
    factory = StaticFactory(fail="the model did not answer (Timeout) after 3 attempts")
    async with harness(factory=factory, vision_hosted_account_daily_requests=1) as h:
        owner = h.owner()
        rig = await h.rig(owner)
        url = f"{API}/mowers/{rig.mower_id}/recover"
        created = (await h.client.post(url, json={}, headers=owner.headers)).json()
        failed = await h.until(created["id"], owner, ProposalState.FAILED)
        assert "Timeout" in failed["detail"]
        [request] = h.store.requests
        assert request.outcome == "error"
        over = await h.client.post(url, json={}, headers=owner.headers)
        assert over.status_code == 429
        assert "configure your own provider" in over.json()["detail"]


async def test_vision_flow_uploads_and_gateway_photo() -> None:
    async with harness() as h:
        owner = h.owner()
        rig = await h.rig(owner)
        # Without images the gateway's photo is used and kept for the proposal's lifetime.
        created = (
            await h.client.post(
                f"{API}/mowers/{rig.mower_id}/recover", json={}, headers=owner.headers
            )
        ).json()
        proposal = await h.until(created["id"], owner, ProposalState.PROPOSED)
        assert proposal["image_count"] == 1
        image = await h.client.get(
            f"{API}/proposals/{created['id']}/images/0", headers=owner.headers
        )
        assert image.status_code == 200 and image.headers["content-type"] == "image/jpeg"
        assert image.content[:2] == b"\xff\xd8"
        assert (
            await h.client.get(f"{API}/proposals/{created['id']}/images/1", headers=owner.headers)
        ).status_code == 404
        await h.client.post(f"{API}/proposals/{created['id']}/reject", headers=owner.headers)
        # Multipart upload.
        upload = await h.client.post(
            f"{API}/mowers/{rig.mower_id}/recover/upload",
            data={"task": "recover"},
            files=[("images", ("snap.png", flat_png(8, 6, (9, 9, 9)), "image/png"))],
            headers=owner.headers,
        )
        assert upload.status_code == 202, upload.text
        body = await h.until(upload.json()["id"], owner, ProposalState.PROPOSED)
        assert body["image_count"] == 1
        await h.client.post(f"{API}/proposals/{body['id']}/reject", headers=owner.headers)
        bad = await h.client.post(
            f"{API}/mowers/{rig.mower_id}/recover/upload",
            files=[("images", ("x.gif", b"GIF89a", "image/gif"))],
            headers=owner.headers,
        )
        assert bad.status_code == 422
        broken = await h.client.post(
            f"{API}/mowers/{rig.mower_id}/recover",
            json={"images": [{"media_type": "image/png", "data": "not base64!"}]},
            headers=owner.headers,
        )
        assert broken.status_code == 422
        too_many = await h.client.post(
            f"{API}/mowers/{rig.mower_id}/recover",
            json={"images": [{"media_type": "image/png", "data": _png_b64()}] * 5},
            headers=owner.headers,
        )
        assert too_many.status_code == 422
        # Without photos from the gateway the context alone is analysed.
        rig.photos = False
        blind = (
            await h.client.post(
                f"{API}/mowers/{rig.mower_id}/recover", json={}, headers=owner.headers
            )
        ).json()
        assert (await h.until(blind["id"], owner, ProposalState.PROPOSED))["image_count"] == 0


async def test_vision_flow_sweep_expires_and_purges() -> None:
    async with harness(vision_proposal_ttl_seconds=30) as h:
        owner = h.owner()
        rig = await h.rig(owner)
        created = (
            await h.client.post(
                f"{API}/mowers/{rig.mower_id}/recover", json={}, headers=owner.headers
            )
        ).json()
        await h.until(created["id"], owner, ProposalState.PROPOSED)
        pid = __import__("uuid").UUID(created["id"])
        stored = h.store.proposals[pid]
        long_ago = stored.created_at.replace(year=2020)
        stored.updated_at = long_ago
        stored.expires_at = long_ago
        for key, image in list(h.store.images.items()):
            h.store.images[key] = image.__class__(
                image.proposal_id, image.index, image.media_type, image.data, image.source, long_ago
            )
        counts = await h.service.sweep()
        assert counts["expired"] == 1 and counts["images"] == 1
        assert h.store.proposals[pid].state is ProposalState.EXPIRED
        # Interrupted work of a crashed instance is given up.
        stuck = h.store.proposals[pid]
        stuck.state = ProposalState.EXECUTING
        stuck.updated_at = long_ago
        assert (await h.service.sweep())["aborted"] == 1
        assert command_subject(rig.mower_id)  # the rig stays untouched
