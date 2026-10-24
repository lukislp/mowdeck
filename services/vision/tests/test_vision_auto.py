"""Automatic AI analysis on mower errors (ADR 0035): opt-in, trigger, limits, entitlements.

The trigger runs against the real app on the in-memory bus: a simulated mower that gets stuck
publishes an error state, the durable consumer ``vision-auto`` hands it to the automatic
analysis, which takes the gateway's photo and creates a proposal like a manual request (the
fake provider answers). Limits are checked through ``AutoAnalysis.trigger`` with crafted states.
"""

import asyncio
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from mowdeck_contracts.v1 import (
    VISION_PROPOSAL_CREATED,
    EventEnvelope,
    MowerActivity,
    MowerState,
    PrincipalKind,
    VisionTrigger,
    utc_now,
)
from mowdeck_contracts.v1.subjects import event_subject_for_type
from mowdeck_service_kit.testing import FakeEntitlements
from vision_support import API, Harness, Person, SimRig, harness, wait_for

from vision.application.auto import AutoAnalysis
from vision.domain.auto import (
    DRIVE_RECOVERABLE,
    Skip,
    normalise_code,
    parse_codes,
    recoverable_error,
)
from vision.domain.models import AutoSetting, Proposal, ProposalState
from vision.settings import Settings

CODES = parse_codes(Settings.model_fields["vision_auto_error_codes"].default)


def _auto(h: Harness) -> AutoAnalysis:
    auto: AutoAnalysis = h.app.state.auto_analysis
    return auto


def _error(rig: SimRig, code: str | None = "1201", **changes: Any) -> MowerState:
    values: dict[str, Any] = {
        "activity": MowerActivity.ERROR,
        "error_code": code,
        "observed_at": utc_now(),
        **changes,
    }
    return rig.sim.snapshot().model_copy(update=values)


def _state(
    mower_id: UUID, account_id: UUID, activity: MowerActivity, code: str | None = None, **kw: Any
) -> MowerState:
    return MowerState.model_validate(
        {
            "mower_id": mower_id,
            "account_id": account_id,
            "observed_at": utc_now(),
            "activity": activity,
            "battery_percent": 80,
            "connection": "simulated",
            "error_code": code,
            **kw,
        }
    )


async def _opt_in(h: Harness, person: Person, *, enabled: bool = True) -> dict[str, Any]:
    response = await h.client.put(
        f"{API}/me/preferences", json={"auto_analysis": enabled}, headers=person.headers
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def _store_opt_in(h: Harness, person: Person, kind: PrincipalKind | None = None) -> None:
    await h.store.put_auto_setting(
        AutoSetting(
            user_id=person.principal.subject,
            account_id=person.account_id,
            enabled=True,
            updated_at=utc_now() - timedelta(minutes=len(h.store.auto) + 1),
            kind=kind or person.principal.kind,
        )
    )


def _age(h: Harness, proposal_id: UUID, minutes: float) -> None:
    stored = h.store.proposals[proposal_id]
    stored.created_at = stored.created_at - timedelta(minutes=minutes)


# --- domain ---------------------------------------------------------------------------------


def test_vision_auto_codes_are_normalised() -> None:
    assert normalise_code(" -1201 ") == "1201"
    assert normalise_code("Stuck") == "stuck"
    assert parse_codes("1201, -1032,,STUCK") == frozenset({"1201", "1032", "stuck"})
    assert parse_codes(["a", " "]) == frozenset({"a"})
    for code in ("stuck", "1201", "1214", "11112", "11121", "1145", "1032", "11134", "2000701"):
        assert code in CODES
    for code in ("1004", "1030", "2502", "2801", "11131", "11133", "1005", "1202", "lifted"):
        assert code not in CODES, f"{code} must never start an automatic analysis"


def test_vision_auto_recoverable_errors() -> None:
    mower, account = uuid4(), uuid4()
    error = MowerActivity.ERROR
    assert recoverable_error(_state(mower, account, error, "-1201"), CODES) == "1201"
    assert recoverable_error(_state(mower, account, error, "stuck"), CODES) == "stuck"
    assert recoverable_error(_state(mower, account, MowerActivity.IDLE, "1201"), CODES) is None
    assert recoverable_error(_state(mower, account, error, "1005"), CODES) is None  # battery
    assert recoverable_error(_state(mower, account, error, "1202"), CODES) is None  # blade
    lifted = _state(mower, account, error, "1201", raw={"lifted": True})
    assert recoverable_error(lifted, CODES) is None
    unknown = _state(mower, account, error, "9999", drive_recoverable=True)
    assert recoverable_error(unknown, CODES) == "9999"
    bare = _state(mower, account, error, None, drive_recoverable=True)
    assert recoverable_error(bare, CODES) == DRIVE_RECOVERABLE


def test_vision_auto_trapped_mower_that_reports_a_pause_triggers() -> None:
    """The Mammotion adapter reports a trapped mower (``MODE_PAUSE`` with fault 1201 in its
    error log) as ``error`` with the code, its category and ``drive_recoverable``; an obstructed
    recharge route (1214) likewise. A lift stop never triggers, also not while paused."""
    mower, account = uuid4(), uuid4()
    error = MowerActivity.ERROR
    trapped = _state(
        mower,
        account,
        error,
        "1201",
        error_message="The robot is stuck",
        drive_recoverable=True,
        raw={"work_mode": "MODE_PAUSE", "error_category": "trapped", "error_source": "error_log"},
    )
    assert recoverable_error(trapped, CODES) == "1201"
    obstructed = _state(
        mower,
        account,
        error,
        "1214",
        raw={"work_mode": "MODE_PAUSE", "error_category": "obstacle"},
    )
    assert recoverable_error(obstructed, CODES) == "1214"
    lifted = _state(
        mower,
        account,
        error,
        "2801",
        raw={"work_mode": "MODE_PAUSE", "error_category": "lift_tilt", "lifted": True},
    )
    assert recoverable_error(lifted, CODES) is None
    paused = _state(mower, account, MowerActivity.PAUSED, None, raw={"work_mode": "MODE_PAUSE"})
    assert recoverable_error(paused, CODES) is None


# --- opt-in ---------------------------------------------------------------------------------


async def test_vision_auto_opt_in_defaults_off_and_needs_the_plan() -> None:
    async with harness() as h:
        owner = h.owner()
        response = await h.client.get(f"{API}/me/preferences", headers=owner.headers)
        assert response.status_code == 200
        assert response.json() == {
            "stereo_frames": True,
            "updated_at": None,
            "auto_analysis": False,
            "auto_analysis_available": True,
            "auto_cooldown_minutes": 10,
            "auto_daily_per_mower": 20,
        }
        assert (await _opt_in(h, owner))["auto_analysis"] is True
        stored = h.store.auto[(owner.principal.subject, owner.account_id)]
        assert stored.enabled and stored.kind is PrincipalKind.USER
        stereo_off = await h.client.put(
            f"{API}/me/preferences", json={"stereo_frames": False}, headers=owner.headers
        )
        assert stereo_off.json()["auto_analysis"] is True, "a field left out keeps its value"
        unchanged = await h.client.put(f"{API}/me/preferences", json={}, headers=owner.headers)
        assert unchanged.json()["auto_analysis"] is True
        assert unchanged.json()["stereo_frames"] is False
        assert (await _opt_in(h, owner, enabled=False))["auto_analysis"] is False

    async with harness(entitlements=FakeEntitlements(features=["control"])) as h:
        owner = h.owner()
        refused = await h.client.put(
            f"{API}/me/preferences", json={"auto_analysis": True}, headers=owner.headers
        )
        assert refused.status_code == 403
        assert refused.json()["type"].endswith("feature-not-in-plan")
        assert (await _opt_in(h, owner, enabled=False))["auto_analysis"] is False

    async with harness(vision_auto_enabled=False, vision_auto_cooldown_minutes=30) as h:
        owner = h.owner()
        body = (await h.client.get(f"{API}/me/preferences", headers=owner.headers)).json()
        assert body["auto_analysis_available"] is False and body["auto_cooldown_minutes"] == 30


# --- end to end -----------------------------------------------------------------------------


async def test_vision_auto_stuck_mower_gets_a_proposal_waiting_for_approval() -> None:
    async with harness() as h:
        owner = h.owner()
        await _opt_in(h, owner)
        rig = await h.rig(owner)
        rig.sim.inject_stuck()
        await wait_for(lambda: len(h.store.proposals) == 1)
        [proposal_id] = list(h.store.proposals)
        body = await h.until(str(proposal_id), owner, ProposalState.PROPOSED)
        assert body["trigger"] == "error"
        assert body["trigger_error_code"] == "stuck"
        assert body["requested_by"]["subject"] == str(owner.principal.subject)
        assert body["image_count"] == 1, "the gateway's photo"
        assert body["task"] == "recover"
        events = [
            EventEnvelope[dict[str, Any]].model_validate_json(m.data)
            for m in h.bus.published
            if m.subject == event_subject_for_type(VISION_PROPOSAL_CREATED)
        ]
        [event] = events
        assert event.payload["trigger"] == "error"
        assert event.payload["error_code"] == "stuck"
        assert event.payload["link"] == f"/mowers/{rig.mower_id}?assist={proposal_id}"
        # The error lasts (heartbeats every 0.25 s): still one analysis, nothing driven.
        await asyncio.sleep(0.8)
        await h.service.idle()
        assert len(h.store.proposals) == 1
        assert h.fleet.starts == []
        assert len(h.factory.calls) == 1
        # The list of the mower's proposals shows the trigger too.
        manual = await h.client.get(f"{API}/mowers/{rig.mower_id}/proposals", headers=owner.headers)
        assert [p["trigger"] for p in manual.json()["items"]] == ["error"]


async def test_vision_auto_needs_an_opt_in_and_the_installation_switch() -> None:
    async with harness() as h:
        owner = h.owner()
        rig = await h.rig(owner)
        rig.sim.inject_stuck()
        await asyncio.sleep(0.8)
        await h.service.idle()
        assert h.store.proposals == {}
        assert h.store.watch[rig.mower_id] == "stuck", "the error was seen"

    async with harness(vision_auto_enabled=False) as h:
        owner = h.owner()
        await _store_opt_in(h, owner)
        rig = await h.rig(owner)
        rig.sim.inject_stuck()
        await asyncio.sleep(0.8)
        assert h.store.proposals == {}
        assert rig.mower_id not in h.store.watch, "no vision-auto consumer"
        assert await _auto(h).trigger(_error(rig), "1201") is Skip.DISABLED


# --- limits ---------------------------------------------------------------------------------


async def test_vision_auto_cooldown_open_proposal_and_daily_budget() -> None:
    async with harness(vision_auto_cooldown_minutes=1, vision_auto_daily_per_mower=2) as h:
        owner = h.owner()
        await _store_opt_in(h, owner)
        rig = await h.rig(owner)
        auto = _auto(h)
        first = await auto.trigger(_error(rig), "1201")
        assert isinstance(first, Proposal)
        assert first.trigger is VisionTrigger.ERROR and first.trigger_error_code == "1201"
        assert await auto.trigger(_error(rig), "1201") is Skip.COOLDOWN
        _age(h, first.id, 2)
        assert await auto.trigger(_error(rig), "1201") is Skip.OPEN_PROPOSAL
        await h.until(str(first.id), owner, ProposalState.PROPOSED)
        rejected = await h.client.post(f"{API}/proposals/{first.id}/reject", headers=owner.headers)
        assert rejected.status_code == 200
        second = await auto.trigger(_error(rig), "11112")
        assert isinstance(second, Proposal)
        await h.until(str(second.id), owner, ProposalState.PROPOSED)
        await h.client.post(f"{API}/proposals/{second.id}/reject", headers=owner.headers)
        _age(h, second.id, 2)
        assert await auto.trigger(_error(rig), "1201") is Skip.DAILY_BUDGET
        # Manual analyses count for the cooldown, not for the automatic budget.
        assert len([p for p in h.store.proposals.values() if p.trigger is VisionTrigger.ERROR]) == 2


async def test_vision_auto_manual_analysis_counts_for_the_cooldown() -> None:
    async with harness() as h:
        owner = h.owner()
        await _store_opt_in(h, owner)
        rig = await h.rig(owner)
        started = await h.client.post(
            f"{API}/mowers/{rig.mower_id}/recover", json={"task": "recover"}, headers=owner.headers
        )
        assert started.status_code == 202, started.text
        await h.until(started.json()["id"], owner, ProposalState.PROPOSED)
        assert started.json()["trigger"] == "manual"
        await h.client.post(f"{API}/proposals/{started.json()['id']}/reject", headers=owner.headers)
        assert await _auto(h).trigger(_error(rig), "1201") is Skip.COOLDOWN


async def test_vision_auto_stale_states_never_trigger() -> None:
    async with harness() as h:
        owner = h.owner()
        await _store_opt_in(h, owner)
        rig = await h.rig(owner)
        old = _error(rig, observed_at=utc_now() - timedelta(minutes=10))
        assert await _auto(h).trigger(old, "1201") is Skip.STALE


# --- who pays and who may ---------------------------------------------------------------------


async def test_vision_auto_needs_the_plan_feature() -> None:
    async with harness(entitlements=FakeEntitlements(features=["control"])) as h:
        owner = h.owner()
        await _store_opt_in(h, owner)
        rig = await h.rig(owner)
        assert await _auto(h).trigger(_error(rig), "1201") is Skip.NOT_IN_PLAN
        assert h.store.proposals == {}


async def test_vision_auto_requester_must_drive_and_have_a_provider() -> None:
    async with harness() as h:
        owner = h.owner()
        rig = await h.rig(owner)
        viewer = h.viewer(owner, rig.mower_id)
        await _store_opt_in(h, viewer)
        auto = _auto(h)
        assert await auto.trigger(_error(rig), "1201") is Skip.NO_SUBSCRIBER
        await _store_opt_in(h, owner)  # opted in after the viewer: asked second
        proposal = await auto.trigger(_error(rig), "1201")
        assert isinstance(proposal, Proposal)
        assert proposal.requested_by.subject == owner.principal.subject

    async with harness(vision_default_enabled=False) as h:
        owner = h.owner()
        await _store_opt_in(h, owner)
        rig = await h.rig(owner)
        assert await _auto(h).trigger(_error(rig), "1201") is Skip.NO_SUBSCRIBER

    async with harness(vision_user_daily_requests=0) as h:
        owner = h.owner()
        await _store_opt_in(h, owner)
        rig = await h.rig(owner)
        assert await _auto(h).trigger(_error(rig), "1201") is Skip.NO_SUBSCRIBER
        assert h.factory.calls == []


async def test_vision_auto_without_photo_does_not_analyse_but_records_why() -> None:
    async with harness(vision_photo_timeout_seconds=0.3) as h:
        owner = h.owner()
        await _store_opt_in(h, owner)
        rig = await h.rig(owner, photos=False)
        assert await _auto(h).trigger(_error(rig), "1201") is Skip.NO_PHOTO
        assert h.factory.calls == [], "no model without a photo"
        [failed] = h.store.proposals.values()
        assert failed.state is ProposalState.FAILED and failed.trigger is VisionTrigger.ERROR
        assert failed.detail is not None and failed.detail.startswith("no photo")


# --- edge detection ---------------------------------------------------------------------------


async def test_vision_auto_only_entering_an_error_counts() -> None:
    async with harness() as h:
        auto = _auto(h)
        mower, account = uuid4(), uuid4()
        idle = _state(mower, account, MowerActivity.IDLE)
        assert await auto.on_state(idle) is False
        assert await auto.on_state(_state(mower, account, MowerActivity.ERROR, "1201")) is True
        assert await auto.on_state(_state(mower, account, MowerActivity.ERROR, "1201")) is False
        assert await auto.on_state(_state(mower, account, MowerActivity.ERROR, "11112")) is True
        assert await auto.on_state(idle) is False
        assert h.store.watch[mower] is None
        assert await auto.on_state(_state(mower, account, MowerActivity.ERROR, "-1201")) is True
        lifted = _state(mower, account, MowerActivity.ERROR, "2801", raw={"lifted": True})
        assert await auto.on_state(lifted) is False
        battery = _state(mower, account, MowerActivity.ERROR, "1005")
        assert await auto.on_state(battery) is False
        await h.service.idle()
        assert h.store.proposals == {}, "nobody opted in"
