"""``vision.proposal.created`` becomes ``vision_proposal`` for the users who may drive."""

from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from mowdeck_contracts.v1 import (
    VISION_PROPOSAL_CREATED,
    VISION_PROPOSAL_UPDATED,
    EventEnvelope,
    NotificationKind,
    NotificationSeverity,
    PrincipalKind,
    PrincipalRef,
    VisionPhotoProblem,
    VisionProposalCreated,
    VisionProposalState,
    VisionProposalUpdated,
    VisionTask,
    VisionTrigger,
)
from mowdeck_service_kit.bus import publish_event
from notifications_support import Harness, harness

KINDS = ["mower_stuck", "vision_proposal"]


def _proposal(
    h: Harness, mower_id: UUID, account_id: UUID, **changes: Any
) -> VisionProposalCreated:
    proposal_id = uuid4()
    values: dict[str, Any] = {
        "proposal_id": proposal_id,
        "mower_id": mower_id,
        "account_id": account_id,
        "task": VisionTask.RECOVER,
        "round": 1,
        "requested_by": PrincipalRef(subject=uuid4(), kind=PrincipalKind.USER),
        "situation": "stuck_on_obstacle",
        "confidence": 0.82,
        "steps": 3,
        "expires_at": h.clock.now + timedelta(minutes=5),
        "link": f"/mowers/{mower_id}?assist={proposal_id}",
        "created_at": h.clock.now,
        **changes,
    }
    return VisionProposalCreated.model_validate(values)


async def _publish(h: Harness, payload: VisionProposalCreated, account_id: UUID) -> None:
    await publish_event(
        h.bus,
        EventEnvelope[VisionProposalCreated](
            type=VISION_PROPOSAL_CREATED,
            producer="vision",
            account_id=account_id,
            payload=payload,
        ),
    )


async def test_notifications_vision_proposal_reaches_drivers_only() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner, name="Vorgarten")
        viewer = await h.member(owner, perms=["mower.view", "notifications.manage"])
        await h.grant_view(viewer, mower_id)
        await h.subscribe_all(owner, enabled_kinds=KINDS, language="de")
        await h.subscribe_all(viewer, enabled_kinds=KINDS)
        proposal = _proposal(h, mower_id, owner.account_id)
        await _publish(h, proposal, owner.account_id)
        await _publish(h, proposal, owner.account_id)  # redelivered: stored once
        [item] = await h.inbox(owner)
        assert item["kind"] == "vision_proposal"
        assert item["url"].endswith(f"/mowers/{mower_id}?assist={proposal.proposal_id}")
        assert item["title"] == "Vorgarten: Vorschlag der KI-Hilfe"
        assert "3 Schritten" in item["body"] and "82 %" in item["body"]
        assert "5 Min." in item["body"]
        assert await h.inbox(viewer) == [], "a viewer without can_drive is not asked to approve"
        find_dock = _proposal(h, mower_id, owner.account_id, task=VisionTask.FIND_DOCK)
        await _publish(h, find_dock, owner.account_id)
        items = await h.inbox(owner)
        assert len(items) == 2
        assert any("Ladestation" in i["body"] for i in items)


async def test_notifications_vision_proposal_of_another_account_is_dropped() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await h.subscribe_all(owner, enabled_kinds=KINDS)
        stranger = uuid4()
        before = len(h.bus.dropped)
        await _publish(h, _proposal(h, mower_id, stranger), stranger)
        await _publish(h, _proposal(h, mower_id, owner.account_id), stranger)  # envelope differs
        await h.bus.publish("event.vision.proposal.created", b"{}")
        assert len(h.bus.dropped) == before + 3
        assert await h.inbox(owner) == []


async def test_notifications_vision_return_to_dock_proposal_says_so() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner, name="Vorgarten")
        await h.subscribe_all(owner, enabled_kinds=[*KINDS, "vision_auto_proposal"])
        dock = _proposal(
            h,
            mower_id,
            owner.account_id,
            task=VisionTask.FIND_DOCK,
            steps=0,
            action="return_to_dock",
            fallback=True,
        )
        await _publish(h, dock, owner.account_id)
        automatic = _proposal(
            h,
            mower_id,
            owner.account_id,
            steps=0,
            action="return_to_dock",
            trigger=VisionTrigger.ERROR,
            error_code="1201",
        )
        await _publish(h, automatic, owner.account_id)
        items = await h.inbox(owner)
        assert len(items) == 2
        assert all("return to the dock on the mapped paths" in i["body"].lower() for i in items)
        assert not any("steps" in i["body"] for i in items)


async def test_notifications_vision_auto_analysis_without_photo_says_why() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner, name="Vorgarten")
        await h.subscribe_all(owner, enabled_kinds=[*KINDS, "vision_auto_proposal"], language="de")
        proposal_id = uuid4()
        failed = VisionProposalUpdated(
            proposal_id=proposal_id,
            mower_id=mower_id,
            account_id=owner.account_id,
            task=VisionTask.RECOVER,
            round=1,
            state=VisionProposalState.FAILED,
            final=True,
            link=f"/mowers/{mower_id}?assist={proposal_id}",
            trigger=VisionTrigger.ERROR,
            error_code="1201",
            photo_problem=VisionPhotoProblem.WEAK_WIFI,
            signal_dbm=-79,
            updated_at=h.clock.now,
        )
        await _publish_update(h, failed, owner.account_id)
        # Any other update (a manual failure, an automatic one with a photo) notifies nobody.
        await _publish_update(
            h,
            failed.model_copy(update={"proposal_id": uuid4(), "photo_problem": None}),
            owner.account_id,
        )
        await _publish_update(
            h,
            failed.model_copy(update={"proposal_id": uuid4(), "trigger": VisionTrigger.MANUAL}),
            owner.account_id,
        )
        [item] = await h.inbox(owner)
        assert item["kind"] == "vision_auto_proposal"
        assert item["title"] == "Vorgarten: KI-Analyse ohne Kamerabild"
        assert "Fehler 1201" in item["body"] and "WLAN" in item["body"]
        assert "-79 dBm" in item["body"] and "von Hand" in item["body"]
        assert item["url"].endswith(f"/mowers/{mower_id}?assist={proposal_id}")


async def _publish_update(h: Harness, payload: VisionProposalUpdated, account_id: UUID) -> None:
    await publish_event(
        h.bus,
        EventEnvelope[VisionProposalUpdated](
            type=VISION_PROPOSAL_UPDATED,
            producer="vision",
            account_id=account_id,
            payload=payload,
        ),
    )


async def test_notifications_vision_auto_proposal_names_the_error() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner, name="Vorgarten")
        await h.subscribe_all(owner, enabled_kinds=[*KINDS, "vision_auto_proposal"], language="de")
        automatic = _proposal(
            h, mower_id, owner.account_id, trigger=VisionTrigger.ERROR, error_code="1201"
        )
        await _publish(h, automatic, owner.account_id)
        await _publish(h, automatic, owner.account_id)  # redelivered: stored once
        [item] = await h.inbox(owner)
        assert item["kind"] == "vision_auto_proposal"
        assert item["title"] == "KI-Vorschlag für Vorgarten"
        assert "Fehler 1201" in item["body"] and "Freigabe" in item["body"]
        assert item["url"].endswith(f"/mowers/{mower_id}?assist={automatic.proposal_id}")


async def test_notifications_vision_auto_proposal_follows_its_own_kind() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await h.subscribe_all(owner, enabled_kinds=KINDS, language="en")
        automatic = _proposal(
            h, mower_id, owner.account_id, trigger=VisionTrigger.ERROR, error_code="stuck"
        )
        await _publish(h, automatic, owner.account_id)
        assert await h.inbox(owner) == [], "vision_auto_proposal is not enabled"
        await h.subscribe_all(owner, enabled_kinds=["vision_auto_proposal"], language="en")
        await _publish(
            h,
            _proposal(
                h, mower_id, owner.account_id, trigger=VisionTrigger.ERROR, error_code="stuck"
            ),
            owner.account_id,
        )
        [item] = await h.inbox(owner)
        assert item["title"].startswith("AI proposal for")
        assert "error stuck" in item["body"]


def test_notifications_vision_auto_proposal_is_a_default_warning() -> None:
    from notifications.domain.models import DEFAULT_KINDS, SEVERITY

    kind = NotificationKind.VISION_AUTO_PROPOSAL
    assert kind in DEFAULT_KINDS
    assert SEVERITY[kind] is NotificationSeverity.WARNING


async def test_notifications_vision_proposal_is_raised_for_the_account_with_can_drive() -> None:
    from mowdeck_contracts.v1 import NotificationRaised

    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner, name="Vorgarten")
        proposal = _proposal(h, mower_id, owner.account_id)
        await _publish(h, proposal, owner.account_id)  # nobody subscribed to anything
        raised = [
            m.model(EventEnvelope[NotificationRaised]).payload
            for m in h.bus.published
            if m.subject == "event.notifications.raised"
        ]
        assert [r.kind for r in raised] == [NotificationKind.VISION_PROPOSAL]
        assert raised[0].required_relation == "can_drive"
        assert raised[0].severity is NotificationSeverity.WARNING
        assert raised[0].title == "Vorgarten: AI assist has a proposal"
