# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""The ``EVENTS`` stream holds nothing of an erased account or a deleted user (ADR 0082).

The stream keeps every domain event for 30 days and its subjects name no account, so Identity
(which owns accounts and users) reads the stream and erases what the envelope or the payload
says is theirs. The rule is pure (``identity.domain.erasure``); the flow runs over the in-memory
bus, which keeps the streams' messages like JetStream does."""

import json
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from identity_support import PASSWORD, bearer, harness
from mowdeck_contracts.v1 import (
    IDENTITY_USER_CREATED,
    IDENTITY_USER_DELETED,
    USER_CHANGED_VERSION,
    EventEnvelope,
    UserChanged,
)
from mowdeck_service_kit.bus import BusError, BusMessage, DropMessage, InMemoryBus
from test_identity_erasure import NOW_GRACE, FakeEraser, _erasure, _finish, _record, _schedule

from identity.adapters.bus.event_log import BusEventLog, user_deleted_handler
from identity.application.erasure import EVENT_STREAM
from identity.domain.erasure import ErasureState, event_names_account, event_names_user

ME = "/api/v1/identity/me"


def envelope(event_type: str, owner: UUID | None, /, **payload: Any) -> bytes:
    """A domain event as a service publishes it (the envelope as JSON)."""
    return json.dumps(
        {
            "id": str(uuid4()),
            "type": event_type,
            "version": 1,
            "account_id": str(owner) if owner else None,
            "producer": "test",
            "payload": payload,
        }
    ).encode()


async def stored(bus: InMemoryBus, subject: str = "event.>") -> list[dict[str, Any]]:
    return [json.loads(m.data) async for m in bus.stored_messages(subject)]


async def traffic(bus: InMemoryBus, account_id: UUID, user_id: UUID, name: str) -> None:
    """What the services leave in ``EVENTS`` about an account and one of its users."""
    acc, uid = str(account_id), str(user_id)
    await bus.publish(
        "event.notifications.raised",
        envelope("notifications.raised", account_id, account_id=acc, title="The mower is stuck"),
    )
    await bus.publish(
        "event.notifications.sent",
        envelope("notifications.sent", account_id, account_id=acc, user_id=uid, body="stuck"),
    )
    await bus.publish(
        "event.mower.registered",
        envelope("mower.registered", account_id, account_id=acc, name="Luba in the garden"),
    )
    await bus.publish(
        "event.tracking.moved",
        envelope("tracking.moved", account_id, account_id=acc, lat=52.5, lon=13.4),
    )
    await bus.publish(
        "event.vision.proposal.created",
        envelope(
            "vision.proposal.created",
            account_id,
            account_id=acc,
            requested_by={"subject": uid, "kind": "user", "display_name": name},
        ),
    )
    await bus.publish(
        "event.identity.session.new_device",
        envelope("identity.session.new_device", account_id, user_id=uid, ip_prefix="203.0.113.0"),
    )


# --- the rules -------------------------------------------------------------------------------


def test_an_event_belongs_to_the_account_its_envelope_or_payload_names() -> None:
    account_id, other = uuid4(), uuid4()
    by_envelope = {"account_id": str(account_id), "payload": {}}
    by_payload = {"account_id": None, "payload": {"account_id": str(account_id).upper()}}
    assert event_names_account(by_envelope, account_id)
    assert event_names_account(by_payload, account_id)
    assert not event_names_account(by_envelope, other)
    assert not event_names_account({"account_id": None, "payload": {"all_subjects": True}}, other)
    assert not event_names_account({"payload": "not an object"}, account_id)
    assert not event_names_account({"account_id": 7, "payload": {"account_id": None}}, account_id)


def test_an_event_names_a_user_by_user_id_or_by_a_principal_reference() -> None:
    user_id, other = uuid4(), uuid4()
    uid = str(user_id)
    assert event_names_user({"type": "notifications.sent", "payload": {"user_id": uid}}, user_id)
    assert event_names_user(
        {"type": "vision.proposal.created", "payload": {"requested_by": {"subject": uid}}}, user_id
    )
    assert event_names_user(
        {"type": "scheduler.schedule.disabled", "payload": {"owner": {"subject": uid}}}, user_id
    )
    assert not event_names_user({"type": "x.y", "payload": {"user_id": str(other)}}, user_id)
    assert not event_names_user({"type": "x.y", "payload": {"subjects": [f"user:{uid}"]}}, user_id)
    assert not event_names_user({"type": "x.y", "payload": None}, user_id)


def test_the_event_that_announces_the_deletion_stays_unless_it_carries_a_name() -> None:
    user_id = uuid4()
    uid = str(user_id)
    nameless = {"type": IDENTITY_USER_DELETED, "payload": {"user_id": uid, "username": None}}
    version_1 = {"type": IDENTITY_USER_DELETED, "payload": {"user_id": uid, "username": "anna"}}
    created = {"type": IDENTITY_USER_CREATED, "payload": {"user_id": uid}}
    assert not event_names_user(nameless, user_id)  # the services still have to read it
    assert event_names_user(version_1, user_id)
    assert event_names_user(created, user_id)


# --- the events Identity publishes -----------------------------------------------------------


async def test_identity_no_longer_publishes_a_user_name() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        user_id, token = await h.subuser(lukas, "anna")
        response = await h.client.request(
            "DELETE", ME, headers=bearer(token), json={"password": PASSWORD}
        )
        assert response.status_code == 200, response.text
        events = [
            json.loads(m.data)
            for m in h.bus.messages("event.identity.user.>")
            if json.loads(m.data)["payload"]["user_id"] in (str(user_id), str(lukas.user_id))
        ]
    assert {e["type"] for e in events} == {IDENTITY_USER_CREATED, IDENTITY_USER_DELETED}
    assert all(e["version"] == USER_CHANGED_VERSION == 2 for e in events)
    assert all("anna" not in json.dumps(e) and "lukas" not in json.dumps(e) for e in events)
    assert all(e["payload"]["username"] is None for e in events)


def test_a_version_1_event_still_decodes() -> None:
    old = envelope(IDENTITY_USER_DELETED, uuid4(), user_id=str(uuid4()), username="anna")
    assert EventEnvelope[UserChanged].model_validate_json(old).payload.username == "anna"


# --- the account erasure ---------------------------------------------------------------------


async def test_an_erased_account_leaves_no_event_in_the_stream() -> None:
    fake = FakeEraser()
    async with harness(eraser=fake, **NOW_GRACE) as h:
        lukas, bob = await h.register("lukas"), await h.register("bob")
        await traffic(h.bus, lukas.account_id, lukas.user_id, "lukas")
        await traffic(h.bus, bob.account_id, bob.user_id, "bob")
        bob_events = [e for e in await stored(h.bus) if event_names_account(e, bob.account_id)]
        await _schedule(h, lukas)

        await _finish(h)

        record = await _record(h, lukas.account_id)
        left = await stored(h.bus)
        async with h.store.transaction() as tx:
            erased = (await tx.audit.find(action="account.erased"))[0]
    assert record.state is ErasureState.ERASED
    # What Identity publishes while it deletes its own rows carries opaque ids only.
    lukas_left = [e for e in left if event_names_account(e, lukas.account_id)]
    assert {e["type"] for e in lukas_left} <= {"identity.grants.changed"}
    assert "lukas" not in json.dumps(left)
    assert [e for e in left if event_names_account(e, bob.account_id)] == bob_events
    # 6 of the services, plus Identity's own (account and user created, grants, the login).
    assert erased.details["counts"][EVENT_STREAM] >= 8


async def test_the_account_stays_locked_while_its_events_cannot_be_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeEraser()

    async def refuse(self: InMemoryBus, message: object) -> bool:
        del self, message
        raise BusError("the stream did not answer")

    async with harness(eraser=fake, **{**NOW_GRACE, "identity_erasure_services": "fleet"}) as h:
        lukas = await h.register("lukas")
        await traffic(h.bus, lukas.account_id, lukas.user_id, "lukas")
        await _schedule(h, lukas)
        with monkeypatch.context() as patched:
            patched.setattr(InMemoryBus, "erase_message", refuse)
            await _finish(h)
        failed = await _record(h, lukas.account_id)
        async with h.store.transaction() as tx:
            account_row = await tx.accounts.get(id=lukas.account_id)
        h.clock.advance(timedelta(minutes=1))
        done = await _erasure(h).sweep()
        record = await _record(h, lukas.account_id)
    assert failed.state is ErasureState.PURGING
    assert (failed.last_error or "").startswith(f"{EVENT_STREAM}: BusError")
    assert account_row is not None  # Identity deleted nothing of its own yet
    assert done.erased == 1 and record.state is ErasureState.ERASED
    assert fake.calls == [("fleet", lukas.account_id)]  # the service is not asked again


# --- a deleted user --------------------------------------------------------------------------


async def test_a_deleted_user_leaves_no_event_that_names_them() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        user_id, token = await h.subuser(lukas, "anna")
        await traffic(h.bus, lukas.account_id, user_id, "anna")
        # An event of before this change: version 1, with the user name.
        await h.bus.publish(
            "event.identity.user.created",
            envelope(
                IDENTITY_USER_CREATED, lukas.account_id, user_id=str(user_id), username="anna"
            ),
        )
        before = await stored(h.bus)

        response = await h.client.request(
            "DELETE", ME, headers=bearer(token), json={"password": PASSWORD}
        )

        assert response.status_code == 200, response.text
        left = await stored(h.bus)
    assert any(event_names_user(e, user_id) for e in before)
    assert not any(event_names_user(e, user_id) for e in left)
    assert "anna" not in json.dumps(left)
    # The announcement itself stays for the services, without a name.
    deleted = [e for e in left if e["type"] == IDENTITY_USER_DELETED]
    assert [e["payload"]["user_id"] for e in deleted] == [str(user_id)]
    # The account goes on: its other events stay.
    kept = {e["type"] for e in left if event_names_account(e, lukas.account_id)}
    assert {"notifications.raised", "mower.registered", "tracking.moved"} <= kept


async def test_a_failed_removal_of_a_users_events_is_delivered_again() -> None:
    user_id = uuid4()
    calls: list[UUID] = []

    class Flaky:
        async def forget_account(self, account_id: UUID) -> int:
            raise AssertionError("not an account erasure")

        async def forget_user(self, user: UUID) -> int:
            calls.append(user)
            if len(calls) == 1:
                raise BusError("the stream did not answer")
            return 3

    bus = InMemoryBus()
    await bus.subscribe("event.identity.user.deleted", user_deleted_handler(Flaky()))
    event = EventEnvelope[UserChanged](
        type=IDENTITY_USER_DELETED, producer="identity", payload=UserChanged(user_id=user_id)
    )

    await bus.publish("event.identity.user.deleted", event)

    assert calls == [user_id, user_id]
    assert bus.dropped == []
    with pytest.raises(DropMessage):
        await user_deleted_handler(Flaky())(BusMessage("event.identity.user.deleted", b"{no"))


async def test_messages_that_are_not_events_stay_and_no_bus_means_nothing_to_erase() -> None:
    bus = InMemoryBus()
    account_id = uuid4()
    await bus.publish("event.odd.raw", b"not json")
    await bus.publish("event.odd.list", b"[1, 2]")
    await bus.publish("event.odd.mine", envelope("odd.mine", account_id))
    log = BusEventLog(bus)

    assert await log.forget_account(account_id) == 1
    assert await log.forget_account(account_id) == 0  # idempotent
    assert await bus.count_subject("event.odd.>") == 2
    assert await BusEventLog(None).forget_account(account_id) == 0
    assert await BusEventLog(None).forget_user(uuid4()) == 0
