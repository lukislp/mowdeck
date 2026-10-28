"""Theft tracking notifications (ADR 0058): events in, inbox items out."""

from typing import Any
from uuid import uuid4

from mowdeck_contracts.v1 import (
    TRACKING_ENDED,
    TRACKING_MOVED,
    TRACKING_STALE,
    TRACKING_STARTED,
    EventEnvelope,
    NotificationKind,
    TrackingEvent,
)
from mowdeck_service_kit.bus import publish_event
from notifications_support import T0, Harness, Person, harness

from notifications.domain.models import QUIET_HOURS_EXEMPT, SEVERITY, TRACKING_KINDS
from notifications.domain.rules import tracking_event

ALL = ["tracking_started", "tracking_moved", "tracking_stale", "tracking_ended"]


async def send(h: Harness, owner: Person, mower_id: Any, event_type: str, **fields: Any) -> None:
    payload = TrackingEvent(
        session_id=fields.pop("session_id", uuid4()),
        mower_id=mower_id,
        account_id=owner.account_id,
        at=fields.pop("at", T0),
        trigger=fields.pop("trigger", "mower_lifted"),
        **fields,
    )
    await publish_event(
        h.bus,
        EventEnvelope[TrackingEvent](
            type=event_type, producer="telemetry", account_id=owner.account_id, payload=payload
        ),
    )


async def test_tracking_events_become_notifications_that_ignore_quiet_hours() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner, name="Backyard")
        await h.subscribe_all(
            owner,
            enabled_kinds=ALL,
            timezone="Europe/Berlin",
            quiet_hours={"start": "13:30", "end": "15:00"},  # T0 is 14:00 in Berlin
            language="de",
        )
        session = uuid4()
        await send(h, owner, mower_id, TRACKING_STARTED, session_id=session, lat=48.1, lon=11.5)
        await send(
            h,
            owner,
            mower_id,
            TRACKING_MOVED,
            session_id=session,
            distance_m=120.4,
            lat=48.2,
            lon=11.5,
        )
        await send(h, owner, mower_id, TRACKING_STALE, session_id=session, stale_minutes=7)
        await send(
            h,
            owner,
            mower_id,
            TRACKING_ENDED,
            session_id=session,
            end_reason="false_alarm",
            track_points=12,
        )
        items = await h.inbox(owner)
        await h.services.outbox.run_once()
    by_kind = {i["kind"]: i for i in items}
    assert sorted(by_kind) == sorted(ALL)
    assert by_kind["tracking_started"]["severity"] == "critical"
    assert "48.10000" in by_kind["tracking_started"]["body"]
    assert by_kind["tracking_started"]["url"].endswith(f"/mowers/{mower_id}/tracking")
    assert "120" in by_kind["tracking_moved"]["body"]
    assert "7" in by_kind["tracking_stale"]["body"]
    assert (
        "Fehlalarm" in by_kind["tracking_ended"]["body"]
        and "12" in by_kind["tracking_ended"]["body"]
    )


async def test_tracking_events_are_deduplicated_and_account_checked() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await h.subscribe_all(owner, enabled_kinds=ALL)
        session = uuid4()
        for _ in range(2):  # a redelivery
            await send(h, owner, mower_id, TRACKING_STARTED, session_id=session)
        other = await h.owner()
        await send(h, other, mower_id, TRACKING_STARTED)  # names a mower of another account
        items = await h.inbox(owner)
        assert [i["kind"] for i in items] == ["tracking_started"]
        assert await h.inbox(other) == []


def test_tracking_rule_text_and_policy() -> None:
    mower = uuid4()
    account = uuid4()
    started = TrackingEvent(
        session_id=uuid4(), mower_id=mower, account_id=account, at=T0, trigger="manual"
    )
    event = tracking_event(TRACKING_STARTED, started)
    assert event.body_key == "tracking_started.body_no_position"
    assert event.link == f"/mowers/{mower}/tracking"
    ended = started.model_copy(update={"end_reason": "max_duration", "track_points": 3})
    assert tracking_event(TRACKING_ENDED, ended).body_key == "tracking_ended.body_max_duration"
    # quiet hours: the live notices ignore them, the end respects them
    ended_kind = NotificationKind.TRACKING_ENDED
    assert (TRACKING_KINDS - {ended_kind}) <= QUIET_HOURS_EXEMPT
    assert ended_kind not in QUIET_HOURS_EXEMPT
    assert all(k in SEVERITY for k in TRACKING_KINDS)
