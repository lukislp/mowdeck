# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""The ``EVENTS`` stream holds nothing of an erased account or a deleted user (ADR 0082).

The stream keeps every domain event for 30 days and its subjects name no account, so Identity
(which owns accounts and users) reads the stream and erases what the envelope or the payload
says is theirs. The rules are pure (``identity.domain.erasure``). The read is cut into slices
with a stored position, so it finishes at any stream size and continues after a restart; that
part runs against a slow fake stream and a fake clock. The flows run over the in-memory bus,
which keeps the streams' messages like JetStream does."""

import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from identity_support import PASSWORD, Harness, bearer, harness
from mowdeck_contracts.v1 import (
    IDENTITY_USER_CREATED,
    IDENTITY_USER_DELETED,
    USERNAME_PLACEHOLDER,
    EventEnvelope,
    UserChanged,
)
from mowdeck_contracts.v1.subjects import subject_matches
from mowdeck_service_kit.bus import BusError, BusMessage, DropMessage, InMemoryBus
from pydantic import Field
from test_identity_erasure import (
    NOW_GRACE,
    FakeEraser,
    _begin,
    _erasure,
    _finish,
    _record,
    _schedule,
)

from identity.adapters.bus.event_log import BusEventLog, user_deleted_handler
from identity.application.erasure import EVENT_STREAM
from identity.application.event_erasure import EventBudget, EventErasureService
from identity.application.ports import StoredEvent
from identity.domain.credentials import validate_username
from identity.domain.erasure import (
    EVENT_FILTERS,
    NO_USERNAME,
    OPAQUE_EVENTS,
    ErasureState,
    EventErasure,
    EventScope,
    event_names_account,
    event_names_user,
)
from identity.domain.errors import InvalidInput

ME = "/api/v1/identity/me"
_NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)


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


def events_of(h: Harness) -> EventErasureService:
    service: EventErasureService | None = h.app.state.identity.event_erasure
    assert service is not None
    return service


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
    assert NO_USERNAME == USERNAME_PLACEHOLDER
    for nameless in (None, USERNAME_PLACEHOLDER):
        announcement = {
            "type": IDENTITY_USER_DELETED,
            "payload": {"user_id": uid, "username": nameless},
        }
        assert not event_names_user(announcement, user_id)  # the services still have to read it
    named = {"type": IDENTITY_USER_DELETED, "payload": {"user_id": uid, "username": "anna"}}
    created = {"type": IDENTITY_USER_CREATED, "payload": {"user_id": uid}}
    assert event_names_user(named, user_id)  # an event of before the name was removed
    assert event_names_user(created, user_id)


def test_the_filters_and_the_skipped_events_do_not_overlap() -> None:
    assert len(set(EVENT_FILTERS)) == len(EVENT_FILTERS)
    assert set(EVENT_FILTERS).isdisjoint(OPAQUE_EVENTS)
    assert all(subject.startswith("event.") for subject in (*EVENT_FILTERS, *OPAQUE_EVENTS))
    assert all(reason for reason in OPAQUE_EVENTS.values())


def test_a_record_reads_filter_by_filter_up_to_its_bound() -> None:
    record = EventErasure.begin(EventScope.USER, uuid4(), _NOW, 500)
    assert (record.filter, record.scanned_sequence, record.finished) == (EVENT_FILTERS[0], 0, False)
    record.scanned(40)
    record.scanned(12)  # never backwards
    assert record.scanned_sequence == 40
    record.next_filter()
    assert (record.filter, record.scanned_sequence) == (EVENT_FILTERS[1], 0)
    for _ in EVENT_FILTERS[1:]:
        record.next_filter()
    assert record.finished and record.last_sequence == 500
    assert EventErasure.begin(EventScope.ACCOUNT, uuid4(), _NOW, -3).last_sequence == 0


# --- the events Identity publishes: no name, and no event lost in any update order ------------


class UserChangedOfThePreviousRelease(UserChanged):
    """``UserChanged`` as the release before this change validates it (v0.69.1, commit
    74312c4d): ``username`` is required and has at least one character. Every consumer of that
    release decodes the payload with it and **drops** (acknowledges and loses) an event that
    does not validate. Frozen here on purpose: it stands for the pods a rolling update has not
    replaced yet."""

    username: str = Field(min_length=1, max_length=64)


def _decodes(model: type[UserChanged], **payload: Any) -> bool:
    try:
        EventEnvelope[model].model_validate_json(  # type: ignore[valid-type]
            envelope(IDENTITY_USER_DELETED, uuid4(), user_id=str(uuid4()), **payload)
        )
    except ValueError:
        return False
    return True


def test_what_a_consumer_of_the_previous_release_does_with_each_shape() -> None:
    old = UserChangedOfThePreviousRelease
    assert not _decodes(old)  # (a) no username: dropped
    assert not _decodes(old, username=None)
    assert not _decodes(old, username="")  # (b) an empty string: dropped
    assert _decodes(old, username=USERNAME_PLACEHOLDER)  # (b) a neutral placeholder: handled
    assert _decodes(old, username="anna")
    # (c) the envelope's version is not looked at: only the payload's shape decides.
    versioned = json.loads(envelope(IDENTITY_USER_DELETED, uuid4(), user_id=str(uuid4())))
    versioned["version"] = 2
    with pytest.raises(ValueError, match="username"):
        EventEnvelope[UserChangedOfThePreviousRelease].model_validate(versioned)
    versioned["payload"]["username"] = USERNAME_PLACEHOLDER
    again = EventEnvelope[UserChangedOfThePreviousRelease].model_validate(versioned)
    assert again.version == 2


def test_the_placeholder_is_nobodys_user_name() -> None:
    with pytest.raises(InvalidInput):
        validate_username(USERNAME_PLACEHOLDER)


def test_this_release_accepts_every_shape() -> None:
    assert _decodes(UserChanged)
    assert _decodes(UserChanged, username=None)
    assert _decodes(UserChanged, username=USERNAME_PLACEHOLDER)
    assert _decodes(UserChanged, username="anna")  # an event of before, still in the stream


async def test_identity_sends_a_placeholder_that_old_and_new_consumers_accept() -> None:
    """Step 1 of the removal (see ``UserChanged``). Do not drop the placeholder or raise the
    version before no installation runs a consumer of v0.69.1 or older: an old consumer would
    lose ``identity.user.deleted`` and keep the user's rows."""
    async with harness() as h:
        lukas = await h.register("lukas")
        user_id, token = await h.subuser(lukas, "anna")
        response = await h.client.request(
            "DELETE", ME, headers=bearer(token), json={"password": PASSWORD}
        )
        assert response.status_code == 200, response.text
        messages = h.bus.messages("event.identity.user.>")
    events = [json.loads(m.data) for m in messages]
    mine = [e for e in events if e["payload"]["user_id"] in (str(user_id), str(lukas.user_id))]
    assert {e["type"] for e in mine} == {IDENTITY_USER_CREATED, IDENTITY_USER_DELETED}
    assert all(e["version"] == 1 for e in mine)
    assert all(e["payload"]["username"] == USERNAME_PLACEHOLDER == "-" for e in mine)
    assert all("anna" not in json.dumps(e) and "lukas" not in json.dumps(e) for e in mine)
    for message in messages:
        EventEnvelope[UserChangedOfThePreviousRelease].model_validate_json(message.data)
        EventEnvelope[UserChanged].model_validate_json(message.data)


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
            open_work = await tx.event_erasures.find()
    assert record.state is ErasureState.ERASED
    # What stays of the account carries ids only (the events that are not read).
    lukas_left = {e["type"] for e in left if event_names_account(e, lukas.account_id)}
    assert lukas_left <= {"identity.grants.changed", "identity.access.revoked"}
    assert "lukas" not in json.dumps(left)
    assert [e for e in left if event_names_account(e, bob.account_id)] == bob_events
    # 6 of the services, plus Identity's own (account and user created, the login).
    assert erased.details["counts"][EVENT_STREAM] >= 8
    assert open_work == []  # the record of the read is gone with the work


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
        # An event of before this change, with the user name.
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
        open_work = await events_of(h).pending_users()
    assert any(event_names_user(e, user_id) for e in before)
    assert not any(event_names_user(e, user_id) for e in left)
    assert "anna" not in json.dumps(left)
    assert open_work == []  # a small stream is done in the first slice
    # The announcement itself stays for the services, without a name.
    deleted = [e for e in left if e["type"] == IDENTITY_USER_DELETED]
    assert [e["payload"]["user_id"] for e in deleted] == [str(user_id)]
    # The account goes on: its other events stay.
    kept = {e["type"] for e in left if event_names_account(e, lukas.account_id)}
    assert {"notifications.raised", "mower.registered", "tracking.moved"} <= kept


async def test_a_deletion_that_could_not_be_recorded_is_delivered_again() -> None:
    user_id = uuid4()
    calls: list[UUID] = []

    async def flaky(user: UUID) -> None:
        calls.append(user)
        if len(calls) == 1:
            raise BusError("the stream did not answer")

    bus = InMemoryBus()
    await bus.subscribe("event.identity.user.deleted", user_deleted_handler(flaky))
    event = EventEnvelope[UserChanged](
        type=IDENTITY_USER_DELETED, producer="identity", payload=UserChanged(user_id=user_id)
    )

    await bus.publish("event.identity.user.deleted", event)

    assert calls == [user_id, user_id]
    assert bus.dropped == []
    with pytest.raises(DropMessage):
        await user_deleted_handler(flaky)(BusMessage("event.identity.user.deleted", b"{no"))


async def test_the_adapter_reads_between_two_sequences_and_skips_what_is_no_event() -> None:
    bus = InMemoryBus()
    account_id = uuid4()
    await bus.publish("event.mower.raw", b"not json")
    await bus.publish("event.mower.list", b"[1, 2]")
    await bus.publish("event.mower.mine", envelope("mower.mine", account_id))
    await bus.publish("event.identity.grants.changed", envelope("identity.grants.changed", None))
    log = BusEventLog(bus)
    end = await log.end()
    await bus.publish("event.mower.late", envelope("mower.late", account_id))

    read = [e async for e in log.read("event.mower.>", 0, end)]

    assert [e.sequence for e in read] == [1, 2, 3]  # not the other subject, not the late one
    assert [e.event is None for e in read] == [True, True, False]
    assert [e.sequence async for e in log.read("event.mower.>", 2, end)] == [3]
    assert await log.erase(read[2]) is True and await log.erase(read[2]) is False
    assert [e async for e in log.read("gateway.mower.x.photo", 0, end)] == []  # not EVENTS
    nothing = BusEventLog(None)
    assert await nothing.end() == 0
    assert [e async for e in nothing.read("event.mower.>", 0, 10)] == []
    assert await nothing.erase(read[0]) is False


# --- a long stream: slices, a stored position, an end ----------------------------------------


class Ticks:
    """A monotonic clock that only the slow stream moves."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class SlowStream:
    """A large ``EVENTS`` stream behind the port: every message read costs time.

    ``size`` events on ``event.notifications.raised``, every tenth of them the account's, and a
    few on other subjects. Reads and erasures are recorded."""

    COST = 0.05

    def __init__(self, ticks: Ticks, account_id: UUID, size: int) -> None:
        self.ticks = ticks
        self.account_id = account_id
        self.events: dict[int, tuple[str, dict[str, Any]]] = {}
        for sequence in range(1, size + 1):
            owner = account_id if sequence % 10 == 0 else uuid4()
            self.events[sequence] = (
                "event.notifications.raised",
                {"account_id": str(owner), "payload": {"title": f"notification {sequence}"}},
            )
        self.reads: list[int] = []
        self.erased: list[int] = []
        self.fail_at: int | None = None

    def publish(self, subject: str, event: dict[str, Any]) -> int:
        sequence = max(self.events, default=0) + 1
        self.events[sequence] = (subject, event)
        return sequence

    async def end(self) -> int:
        return max(self.events, default=0)

    async def read(self, subject: str, after: int, until: int) -> AsyncGenerator[StoredEvent]:
        for sequence in sorted(self.events):
            stored_subject, event = self.events[sequence]
            if (
                sequence <= after
                or sequence > until
                or not subject_matches(subject, stored_subject)
            ):
                continue
            if self.fail_at == sequence:
                self.fail_at = None
                raise BusError("the stream did not answer")
            self.ticks.now += self.COST
            self.reads.append(sequence)
            yield StoredEvent(sequence, event)

    async def erase(self, event: StoredEvent) -> bool:
        self.erased.append(event.sequence)
        del self.events[event.sequence]
        return True


SIZE = 2000  # 100 s of reading at 0.05 s per message: more than three acknowledgement waits


async def test_a_read_longer_than_the_acknowledgement_wait_finishes_in_slices() -> None:
    async with harness() as h:
        ticks, account_id = Ticks(), uuid4()
        stream = SlowStream(ticks, account_id, SIZE)
        service = EventErasureService(
            h.app.state.identity.deps, stream, EventBudget(seconds=10.0), monotonic=ticks
        )
        slices: list[float] = []
        result: int | None = None
        while result is None:
            started = ticks.now
            result = await service.advance(EventScope.ACCOUNT, account_id)
            slices.append(ticks.now - started)
            assert len(slices) < 50, "the read does not end"
        async with h.store.transaction() as tx:
            left = await tx.event_erasures.find()
    assert result == SIZE // 10 == len(stream.erased)
    assert stream.reads == list(range(1, SIZE + 1))  # every message once, never from the start
    assert len(slices) == 10 and max(slices) <= 10.0 + SlowStream.COST  # far below 30 s each
    assert sum(slices) > 90.0  # the whole read is three times the acknowledgement wait
    assert left == []


async def test_a_restart_continues_where_the_read_stopped() -> None:
    async with harness() as h:
        ticks, user_id = Ticks(), uuid4()
        stream = SlowStream(ticks, uuid4(), SIZE)
        deps = h.app.state.identity.deps
        first = EventErasureService(deps, stream, EventBudget(seconds=10.0), monotonic=ticks)
        assert await first.advance(EventScope.USER, user_id) is None
        assert await first.advance(EventScope.USER, user_id) is None
        [position] = await first.pending_users()
        read_before = len(stream.reads)

        # The process ends; another one (or another replica) carries on from the record.
        restarted = EventErasureService(deps, stream, EventBudget(seconds=10.0), monotonic=ticks)
        while await restarted.sweep_users() != (0, 0):
            pass

    assert position.scanned_sequence == read_before == 400
    assert position.last_sequence == SIZE
    assert stream.reads == list(range(1, SIZE + 1))  # nothing was read twice
    assert stream.erased == []  # no event names this user


async def test_events_published_while_reading_do_not_keep_the_read_alive() -> None:
    async with harness() as h:
        ticks, account_id = Ticks(), uuid4()
        stream = SlowStream(ticks, account_id, 300)
        service = EventErasureService(
            h.app.state.identity.deps, stream, EventBudget(seconds=10.0), monotonic=ticks
        )
        slices = 0
        while await service.advance(EventScope.ACCOUNT, account_id) is None:
            slices += 1
            for _ in range(500):  # traffic faster than the read
                stream.publish("event.notifications.raised", {"account_id": str(uuid4())})
            assert slices < 10
    assert max(stream.reads) == 300  # the bound of the moment the work began
    assert len(stream.reads) == 300


async def test_a_failure_in_the_middle_keeps_the_position() -> None:
    async with harness() as h:
        ticks, account_id = Ticks(), uuid4()
        stream = SlowStream(ticks, account_id, 100)
        stream.fail_at = 61
        service = EventErasureService(h.app.state.identity.deps, stream, monotonic=ticks)
        with pytest.raises(BusError):
            await service.advance(EventScope.ACCOUNT, account_id)
        async with h.store.transaction() as tx:
            [position] = await tx.event_erasures.find()
        done = await service.advance(EventScope.ACCOUNT, account_id)
    assert position.scanned_sequence == 60 and position.erased == 6
    assert done == 10
    assert stream.reads == list(range(1, 101))


async def test_only_subjects_that_can_name_somebody_are_read() -> None:
    async with harness() as h:
        ticks, account_id = Ticks(), uuid4()
        stream = SlowStream(ticks, account_id, 0)
        mine = {"account_id": str(account_id), "payload": {}}
        opaque = [stream.publish(subject.replace("*", "x"), mine) for subject in OPAQUE_EVENTS]
        named = stream.publish("event.identity.user.created", mine)
        service = EventErasureService(h.app.state.identity.deps, stream, monotonic=ticks)
        assert await service.advance(EventScope.ACCOUNT, account_id) == 1
    assert stream.reads == [named] and stream.erased == [named]
    assert all(sequence in stream.events for sequence in opaque)


async def test_a_long_read_neither_outlives_the_purges_lease_nor_blocks_other_purges() -> None:
    """Two accounts are purged while the stream takes 25 s to read: every sweep gives each purge
    one slice of 10 s, so no attempt holds its five-minute lease for longer and neither purge
    waits for the other to finish."""
    fake = FakeEraser()
    async with harness(eraser=fake, **{**NOW_GRACE, "identity_erasure_services": "fleet"}) as h:
        lukas, bob = await h.register("lukas"), await h.register("bob")
        ticks = Ticks()
        stream = SlowStream(ticks, lukas.account_id, 500)
        events_of(h).log = stream
        events_of(h).budget = EventBudget(seconds=10.0)
        events_of(h)._monotonic = ticks
        await _schedule(h, lukas)
        await _schedule(h, bob)
        await _begin(h)

        first = await _erasure(h).sweep()
        after_first = [await _record(h, a.account_id) for a in (lukas, bob)]
        async with h.store.transaction() as tx:
            positions = {r.subject_id: r for r in await tx.event_erasures.find()}
        spent_first = ticks.now
        second = await _erasure(h).sweep()
        third = await _erasure(h).sweep()
        done = [await _record(h, a.account_id) for a in (lukas, bob)]

    assert (first.erased, first.pending) == (0, 2)
    assert spent_first <= 2 * (10.0 + SlowStream.COST)  # one slice each, not one whole read
    for record in after_first:
        assert record.state is ErasureState.PURGING
        assert record.attempts == 0 and record.last_error is None  # a slice is not a failure
        assert record.next_attempt_at is not None
        assert record.next_attempt_at <= h.clock()  # due again at once: the next slice
    # Both reads advanced in the same sweep: neither purge waits for the other's read to end.
    assert all(200 <= p.scanned_sequence < 500 for p in positions.values())
    assert set(positions) == {lukas.account_id, bob.account_id}
    assert (second.erased, third.erased) == (0, 2)
    assert all(record.state is ErasureState.ERASED for record in done)
    assert sorted(fake.calls) == sorted([("fleet", lukas.account_id), ("fleet", bob.account_id)])
    assert len(stream.erased) == 50  # lukas's notifications; bob had none in this stream
