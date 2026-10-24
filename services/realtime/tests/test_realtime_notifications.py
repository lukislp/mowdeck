"""``notification`` and ``vision_proposal`` over the socket, with their authorization.

* A person gets the items of their own inbox (``notifications.sent``), without a subscription,
  never another person's and never one of another account.
* An API token has no inbox: it gets the account's notifications (``notifications.raised``)
  only with ``notifications.view`` in the token, only for mowers it subscribed (``can_view``),
  and the AI assist kinds only with ``can_drive``.
* Proposals reach the sessions subscribed to the mower within its account, like states.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from mowdeck_authz import RelationshipTuple, account, principal_subject
from mowdeck_contracts.v1 import (
    NOTIFICATION_RAISED,
    NOTIFICATION_SENT,
    NOTIFICATIONS_VIEW_PERMISSION,
    VISION_PROPOSAL_CREATED,
    VISION_PROPOSAL_UPDATED,
    EventEnvelope,
    NotificationKind,
    NotificationRaised,
    NotificationSent,
    NotificationSeverity,
    Principal,
    PrincipalKind,
    PrincipalRef,
    VisionProposalCreated,
    VisionProposalState,
    VisionProposalUpdated,
    VisionTask,
)
from mowdeck_contracts.v1.realtime import RealtimeServerMessage
from mowdeck_service_kit.bus import publish_event
from pydantic import BaseModel
from realtime_support import Harness, Person, harness

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)


async def api_token(
    h: Harness, of: Person, *, permissions: list[str], drive: bool = False
) -> Person:
    """An API token of ``of``'s account with ``mower.view`` (and ``mower.drive``) on the account
    and the given token permissions."""
    principal = Principal(
        subject=uuid4(),
        account_id=of.account_id,
        kind=PrincipalKind.API_TOKEN,
        display_name="Home Assistant",
        permissions=permissions,
    )
    subject = principal_subject(principal)
    tuples = [
        RelationshipTuple(subject, "member", account(of.account_id)),
        RelationshipTuple(subject, "grant_mower_view", account(of.account_id)),
    ]
    if drive:
        tuples.append(RelationshipTuple(subject, "grant_mower_drive", account(of.account_id)))
    await h.identity.write(tuples)
    return Person(principal, h.keys.mint(principal))


async def _event(h: Harness, event_type: str, account_id: UUID, payload: BaseModel) -> None:
    await publish_event(
        h.bus,
        EventEnvelope[Any](
            type=event_type, producer="test", account_id=account_id, payload=payload
        ),
    )


def sent(person: Person, mower_id: UUID | None, **changes: Any) -> NotificationSent:
    values: dict[str, Any] = {
        "notification_id": uuid4(),
        "user_id": person.principal.subject,
        "account_id": person.account_id,
        "mower_id": mower_id,
        "kind": NotificationKind.MOWING_FINISHED,
        "severity": NotificationSeverity.INFO,
        "title": "Backyard finished mowing",
        "body": "42 min mowed.",
        "url": f"/mowers/{mower_id}",
        "occurred_at": NOW,
        "created_at": NOW + timedelta(seconds=1),
        **changes,
    }
    return NotificationSent.model_validate(values)


def raised(account_id: UUID, mower_id: UUID | None, **changes: Any) -> NotificationRaised:
    values: dict[str, Any] = {
        "notification_id": uuid4(),
        "account_id": account_id,
        "mower_id": mower_id,
        "kind": NotificationKind.MOWER_LIFTED,
        "severity": NotificationSeverity.CRITICAL,
        "title": "Backyard was lifted",
        "body": "The lift sensor stopped the mower.",
        "url": f"/mowers/{mower_id}",
        "occurred_at": NOW,
        "created_at": NOW,
        "dedupe_key": f"mower_lifted:{mower_id}:1",
        **changes,
    }
    return NotificationRaised.model_validate(values)


def created(account_id: UUID, mower_id: UUID) -> VisionProposalCreated:
    proposal_id = uuid4()
    return VisionProposalCreated(
        proposal_id=proposal_id,
        mower_id=mower_id,
        account_id=account_id,
        task=VisionTask.RECOVER,
        round=1,
        requested_by=PrincipalRef(subject=uuid4(), kind=PrincipalKind.USER),
        situation="stuck_on_obstacle",
        confidence=0.8,
        steps=2,
        expires_at=NOW + timedelta(minutes=5),
        link=f"/mowers/{mower_id}?assist={proposal_id}",
        created_at=NOW,
    )


async def test_realtime_person_gets_own_inbox_items_without_subscribing() -> None:
    async with harness() as h:
        owner = await h.owner()
        viewer = await h.viewer(owner)
        mower_id = await h.add_mower(owner)
        mine = await h.connect_as(owner)
        theirs = await h.connect_as(viewer)
        item = sent(owner, mower_id)
        await _event(h, NOTIFICATION_SENT, owner.account_id, item)
        message = await mine.expect("notification")
        RealtimeServerMessage.model_validate(message)
        note = message["notification"]
        assert note["id"] == str(item.notification_id) and note["scope"] == "user"
        assert note["kind"] == "mowing_finished" and note["severity"] == "info"
        assert note["title"] == "Backyard finished mowing" and note["read"] is False
        assert note["created_at"] == "2026-10-22T12:00:01Z"
        await theirs.nothing()


async def test_realtime_inbox_item_of_another_account_is_dropped() -> None:
    async with harness() as h:
        owner = await h.owner()
        client = await h.connect_as(owner)
        # The same subject, but the event names another account (never trusted).
        foreign = sent(owner, None, account_id=uuid4())
        await _event(h, NOTIFICATION_SENT, foreign.account_id, foreign)
        await client.nothing()


async def test_realtime_api_token_gets_account_notifications_with_permission() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        token = await api_token(h, owner, permissions=["mower.view", NOTIFICATIONS_VIEW_PERMISSION])
        blind = await api_token(h, owner, permissions=["mower.view"])
        ha = await h.connect_as(token)
        other = await h.connect_as(blind)
        person = await h.connect_as(owner)
        await ha.subscribe(mower_id)
        await other.subscribe(mower_id)
        await person.subscribe(mower_id)
        alarm = raised(owner.account_id, mower_id)
        await _event(h, NOTIFICATION_RAISED, owner.account_id, alarm)
        message = await ha.expect("notification")
        RealtimeServerMessage.model_validate(message)
        assert message["notification"]["id"] == str(alarm.notification_id)
        assert message["notification"]["scope"] == "account"
        assert message["notification"]["severity"] == "critical"
        await other.nothing()  # no notifications.view in the token
        # People get their own inbox items instead (notifications.sent), never the account's.
        await person.nothing()


async def test_realtime_api_token_needs_the_mower_subscribed_and_same_account() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        unsubscribed = await h.add_mower(owner)
        token = await api_token(h, owner, permissions=[NOTIFICATIONS_VIEW_PERMISSION])
        ha = await h.connect_as(token)
        await ha.subscribe(mower_id)
        await _event(
            h, NOTIFICATION_RAISED, owner.account_id, raised(owner.account_id, unsubscribed)
        )
        stranger = await h.owner()
        foreign = raised(stranger.account_id, mower_id)
        await _event(h, NOTIFICATION_RAISED, stranger.account_id, foreign)
        await ha.nothing()
        # An account-wide notification (no mower) needs no subscription.
        general = raised(owner.account_id, None, kind=NotificationKind.TEST, url=None)
        await _event(h, NOTIFICATION_RAISED, owner.account_id, general)
        message = await ha.expect("notification")
        assert message["notification"]["mower_id"] is None


async def test_realtime_api_token_gets_ai_assist_notifications_only_with_can_drive() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        perms = ["mower.view", NOTIFICATIONS_VIEW_PERMISSION]
        viewer_token = await api_token(h, owner, permissions=perms)
        driver_token = await api_token(h, owner, permissions=[*perms, "mower.drive"], drive=True)
        viewing = await h.connect_as(viewer_token)
        driving = await h.connect_as(driver_token)
        await viewing.subscribe(mower_id)
        await driving.subscribe(mower_id)
        proposal = raised(
            owner.account_id,
            mower_id,
            kind=NotificationKind.VISION_PROPOSAL,
            severity=NotificationSeverity.WARNING,
            required_relation="can_drive",
        )
        await _event(h, NOTIFICATION_RAISED, owner.account_id, proposal)
        message = await driving.expect("notification")
        assert message["notification"]["kind"] == "vision_proposal"
        await viewing.nothing()


async def test_realtime_vision_proposals_reach_subscribers_of_the_account() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        token = await api_token(h, owner, permissions=["mower.view"])
        subscribed = await h.connect_as(owner)
        ha = await h.connect_as(token)
        idle = await h.connect_as(await h.viewer(owner))
        await subscribed.subscribe(mower_id)
        await ha.subscribe(mower_id)
        event = created(owner.account_id, mower_id)
        await _event(h, VISION_PROPOSAL_CREATED, owner.account_id, event)
        for client in (subscribed, ha):
            message = await client.expect("vision_proposal")
            RealtimeServerMessage.model_validate(message)
            assert message["event"] == "created" and message["mower_id"] == str(mower_id)
            proposal = message["proposal"]
            assert proposal["id"] == str(event.proposal_id) and proposal["state"] == "proposed"
            assert proposal["action"] == "drive" and proposal["steps"] == 2
            assert proposal["expires_at"] == "2026-10-22T12:05:00Z"
            assert proposal["final"] is False
            assert "image" not in str(proposal)
        await idle.nothing()  # not subscribed to the mower
        update = VisionProposalUpdated(
            proposal_id=event.proposal_id,
            mower_id=mower_id,
            account_id=owner.account_id,
            task=VisionTask.RECOVER,
            round=1,
            state=VisionProposalState.REJECTED,
            final=True,
            action="drive",
            steps=2,
            link=event.link,
            updated_at=NOW + timedelta(minutes=1),
        )
        await _event(h, VISION_PROPOSAL_UPDATED, owner.account_id, update)
        message = await subscribed.expect("vision_proposal", event="updated")
        assert message["proposal"]["state"] == "rejected" and message["proposal"]["final"] is True
        # A proposal of another account naming this mower id is never handed over.
        stranger = await h.owner()
        foreign = created(stranger.account_id, mower_id)
        await _event(h, VISION_PROPOSAL_CREATED, stranger.account_id, foreign)
        await subscribed.nothing()


async def test_realtime_resume_proposal_carries_its_action() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        client = await h.connect_as(owner)
        await client.subscribe(mower_id)
        event = created(owner.account_id, mower_id).model_copy(
            update={"action": "resume", "steps": 1}
        )
        await _event(h, VISION_PROPOSAL_CREATED, owner.account_id, event)
        message = await client.expect("vision_proposal")
        RealtimeServerMessage.model_validate(message)
        assert message["proposal"]["action"] == "resume" and message["proposal"]["steps"] == 1
        update = VisionProposalUpdated(
            proposal_id=event.proposal_id,
            mower_id=mower_id,
            account_id=owner.account_id,
            task=VisionTask.RECOVER,
            round=1,
            state=VisionProposalState.RECOVERED,
            final=True,
            action="resume",
            steps=1,
            link=event.link,
            updated_at=NOW + timedelta(minutes=1),
        )
        await _event(h, VISION_PROPOSAL_UPDATED, owner.account_id, update)
        done = await client.expect("vision_proposal", event="updated")
        RealtimeServerMessage.model_validate(done)
        assert done["proposal"]["action"] == "resume"
        assert done["proposal"]["state"] == "recovered"


async def test_realtime_low_confidence_choice_carries_both_options() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        client = await h.connect_as(owner)
        await client.subscribe(mower_id)
        event = created(owner.account_id, mower_id).model_copy(
            update={
                "action": "return_to_dock",
                "steps": 0,
                "fallback": True,
                "low_confidence": True,
                "alternative_action": "resume",
                "confidence": 0.55,
            }
        )
        await _event(h, VISION_PROPOSAL_CREATED, owner.account_id, event)
        message = await client.expect("vision_proposal")
        RealtimeServerMessage.model_validate(message)
        summary = message["proposal"]
        # Old readers keep `action` (the default, the fallback); new ones see the choice.
        assert summary["action"] == "return_to_dock" and summary["fallback"] is True
        assert summary["low_confidence"] is True and summary["alternative_action"] == "resume"


async def test_realtime_event_with_mismatching_account_is_dropped() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        client = await h.connect_as(owner)
        await client.subscribe(mower_id)
        before = len(h.bus.dropped)
        event = created(owner.account_id, mower_id)
        await _event(h, VISION_PROPOSAL_CREATED, uuid4(), event)  # envelope of another account
        await client.nothing()
        assert len(h.bus.dropped) == before + 1
