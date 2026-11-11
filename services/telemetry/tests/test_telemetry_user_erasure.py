# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""A deleted user (ADR 0082, "delete me"): on ``identity.user.deleted`` Telemetry revokes the
tracking share links the user created; it stores no name and no row that is a user's own.

The event goes over the in-memory bus to the real app, as Identity publishes it. The stores are
the memory stores and, with ``MOWDECK_TEST_DATABASE_URL``, PostgreSQL.
"""

from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from mowdeck_contracts.v1 import IDENTITY_USER_DELETED, EventEnvelope, UserChanged
from mowdeck_contracts.v1.subjects import event_subject_for_type
from mowdeck_service_kit.db import principal_ref_columns, user_tables
from mowdeck_service_kit.user_erasure import UserErasureIncomplete
from telemetry_support import API, T0, Harness, harness, make_state
from test_telemetry_demo_purge import KINDS, stores_of
from test_telemetry_tracking import Clock, Gateway, public

from telemetry.adapters.bus.user_erasure import USERS_CONSUMER
from telemetry.adapters.db.tables import metadata
from telemetry.adapters.memory_tracking import MemoryTrackingStore
from telemetry.application.tracking_ports import TrackingStore
from telemetry.application.user_erasure import user_erasure
from telemetry.domain.tracking import TrackingLink, TrackingSession, new_token, token_hash

SUBJECT = event_subject_for_type(IDENTITY_USER_DELETED)


async def add_session(tracking: TrackingStore, account_id: UUID) -> TrackingSession:
    session = TrackingSession(
        id=uuid4(),
        account_id=account_id,
        mower_id=uuid4(),
        trigger="manual",
        started_at=T0,
        ends_at=T0 + timedelta(hours=24),
        interval_seconds=10.0,
        mower_name="Front",
    )
    assert await tracking.add_session(session)
    return session


async def add_link(
    tracking: TrackingStore,
    session: TrackingSession,
    creator: UUID | None,
    *,
    hours: float = 48,
    token: str | None = None,
) -> TrackingLink:
    link = TrackingLink(
        id=uuid4(),
        session_id=session.id,
        account_id=session.account_id,
        mower_id=session.mower_id,
        token_hash=token_hash(token or new_token()),
        created_at=T0,
        expires_at=T0 + timedelta(hours=hours),
        created_by=creator,
    )
    await tracking.add_link(link)
    return link


async def deleted(h: Harness, user_id: UUID, account_id: UUID | None) -> None:
    """What Identity publishes when a user is deleted."""
    payload = UserChanged(user_id=user_id, account_id=account_id, username="anna", subuser=True)
    await h.bus.publish(
        SUBJECT,
        EventEnvelope[UserChanged](
            type=IDENTITY_USER_DELETED, producer="identity", account_id=account_id, payload=payload
        ),
    )


# --- the schema ------------------------------------------------------------------------------


def test_telemetry_stores_no_name_and_no_row_that_is_a_users_own() -> None:
    """Who changed a blade, started a tracking session or drew a zone is a plain subject id. A
    column that stores a principal reference (with a name) or a table with a ``user_id`` column
    needs a decision in the user erasure, and this fails until then."""
    assert principal_ref_columns(metadata) == ()
    assert user_tables(metadata) == ()


# --- the store -------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
async def test_the_links_a_user_created_are_revoked_and_nothing_else(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with stores_of(kind, monkeypatch) as (_, _, tracking):
        anna, ben = uuid4(), uuid4()
        mine, other = await add_session(tracking, uuid4()), await add_session(tracking, uuid4())
        annas = [await add_link(tracking, mine, anna), await add_link(tracking, mine, anna)]
        expired = await add_link(tracking, mine, anna, hours=1)
        elsewhere = await add_link(tracking, other, anna)
        kept = [await add_link(tracking, mine, ben), await add_link(tracking, mine, None)]
        now = T0 + timedelta(hours=2)
        usable_before = await tracking.usable_links_of_creator(anna, now)

        in_account = await tracking.revoke_links_of_creator(anna, mine.account_id, now)
        usable_after = await tracking.usable_links_of_creator(anna, now)
        everywhere = await tracking.revoke_links_of_creator(anna, None, now)
        again = await tracking.revoke_links_of_creator(anna, None, now)

        links = {link.id: link for link in await tracking.links_of(mine.id)}
        others = {link.id: link for link in await tracking.links_of(other.id)}
    assert usable_before == 3  # two here and one in the other account; one has expired
    assert in_account == 3  # the expired one is revoked too: it is the user's all the same
    assert usable_after == 1 and (everywhere, again) == (1, 0)
    assert len(links) == 5  # the rows stay
    assert all(links[link.id].revoked_at == now for link in [*annas, expired])
    assert all(links[link.id].revoked_at is None for link in kept)
    assert others[elsewhere.id].revoked_at == now
    assert all(links[link.id] == link for link in kept)  # exactly as they were


# --- the event -------------------------------------------------------------------------------


async def test_a_deleted_members_share_link_stops_working_at_once() -> None:
    """Through the real app: the link somebody created shows the mower to whoever has its
    token. Once that person is deleted it shows nothing; the owner's link goes on."""
    clock = Clock()
    async with harness(clock=clock, tracking_gateway=Gateway()) as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await h.publish_state(make_state(mower_id, owner.account_id, at=T0, position=(48.0, 11.0)))
        base = f"{API}/mowers/{mower_id}/tracking"
        started = await h.client.post(base, headers=owner.headers)
        assert started.status_code in (200, 201), started.text
        owners = (await h.client.post(f"{base}/links", headers=owner.headers, json={})).json()
        tracking: TrackingStore = h.app.state.telemetry.tracking._store
        [session] = await tracking.active_sessions()
        anna, token = uuid4(), new_token()
        await add_link(tracking, session, anna, token=token)
        before = (await public(h, token)).status_code

        await deleted(h, anna, owner.account_id)
        await deleted(h, anna, owner.account_id)  # a redelivered event changes nothing

        after = (await public(h, token)).status_code
        owner_view = (await public(h, owners["token"])).status_code
        listed = (await h.client.get(f"{base}/links", headers=owner.headers)).json()
    assert (before, after, owner_view) == (200, 404, 200)
    assert len(listed["items"] if isinstance(listed, dict) else listed) == 2
    assert h.bus.dropped == []


async def test_links_that_still_work_fail_the_event_and_a_redelivery_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A revocation that leaves a working link behind is never acknowledged as done."""
    async with harness(clock=Clock()) as h:
        tracking: TrackingStore = h.app.state.telemetry.tracking._store
        session = await add_session(tracking, uuid4())
        anna = uuid4()
        await add_link(tracking, session, anna)
        calls: list[int] = []
        real = MemoryTrackingStore.revoke_links_of_creator

        async def flaky(self: Any, user_id: UUID, account_id: UUID | None, now: Any) -> int:
            calls.append(1)
            if len(calls) == 1:
                return 0  # the first attempt revokes nothing
            return int(await real(self, user_id, account_id, now))

        monkeypatch.setattr(MemoryTrackingStore, "revoke_links_of_creator", flaky)
        with pytest.raises(UserErasureIncomplete) as raised:
            await h.app.state.telemetry.user_erasure.erase(anna, session.account_id)
        calls.clear()
        await deleted(h, anna, session.account_id)  # fails once, the bus delivers it again
        [link] = await tracking.links_of(session.id)
    assert raised.value.leftovers == {"tracking_links": 1}
    assert len(calls) == 2
    assert link.revoked_at is not None and h.bus.dropped == []


async def test_a_user_telemetry_never_saw_and_a_malformed_event_are_no_error() -> None:
    async with harness() as h:
        await deleted(h, uuid4(), uuid4())
        await h.bus.publish(SUBJECT, b'{"type": "identity.user.deleted", "payload": {}}')
        dropped = [m.subject for m in h.bus.dropped]
    assert dropped == [SUBJECT]  # the malformed one, not redelivered for ever


async def test_the_use_case_counts_only_links_that_still_work() -> None:
    tracking = MemoryTrackingStore()
    session = await add_session(tracking, uuid4())
    anna = uuid4()
    await add_link(tracking, session, anna, hours=1)
    late = Clock(T0 + timedelta(hours=2))
    assert await user_erasure(tracking, late).erase(anna) == 1  # revoked although expired
    assert await user_erasure(tracking, late).erase(anna) == 0


def test_the_consumer_is_durable_and_shared_by_the_replicas() -> None:
    assert USERS_CONSUMER == "telemetry-users"
