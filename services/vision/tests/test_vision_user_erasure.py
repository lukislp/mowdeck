# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""A deleted user (ADR 0082, "delete me"): on ``identity.user.deleted`` Vision deletes the
person's own rows (their model with its key, preferences with opt-in and consent, the cost rows
of their model calls) and removes their name from the proposals, which stay with the account.

The event goes over the in-memory bus to the real app, as Identity publishes it. The store is the
memory store and, with ``MOWDECK_TEST_DATABASE_URL``, PostgreSQL (see ``test_vision_db``).
"""

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from mowdeck_contracts.v1 import (
    IDENTITY_USER_DELETED,
    EventEnvelope,
    PrincipalKind,
    PrincipalRef,
    UserChanged,
)
from mowdeck_contracts.v1.subjects import event_subject_for_type
from mowdeck_service_kit.bus import EventBus
from mowdeck_service_kit.db import principal_ref_columns, user_tables
from mowdeck_service_kit.user_erasure import UserErasureIncomplete
from test_vision_db import _proposal
from test_vision_demo_purge import holdings, store_of
from test_vision_erasure import CLOUD, KINDS, seed
from vision_support import T0, harness

from vision.adapters.bus.user_erasure import USERS_CONSUMER
from vision.adapters.db.tables import metadata
from vision.adapters.memory import MemoryStore
from vision.application.ports import Store
from vision.domain.consent import Consent
from vision.domain.models import Proposal, ProposalState

SUBJECT = event_subject_for_type(IDENTITY_USER_DELETED)

OWN_ROWS = {"provider_requests", "user_preferences", "user_providers"}
"""The tables of a user's own rows, as the schema gives them (``user_id`` column)."""
NAMES = {"proposals.requested_by", "proposals.approved_by", "proposals.decided_by"}
"""The columns that store a person's name in a principal reference."""


def ref(user_id: UUID, name: str) -> PrincipalRef:
    return PrincipalRef(subject=user_id, kind=PrincipalKind.SUBUSER, display_name=name)


async def deleted(bus: EventBus, user_id: UUID, account_id: UUID | None) -> None:
    """What Identity publishes when a user is deleted."""
    payload = UserChanged(user_id=user_id, account_id=account_id, reason="self")
    await bus.publish(
        SUBJECT,
        EventEnvelope[UserChanged](
            type=IDENTITY_USER_DELETED,
            version=2,
            producer="identity",
            account_id=account_id,
            payload=payload,
        ),
    )


async def decided(store: Store, account_id: UUID, mower_id: UUID, by: PrincipalRef) -> Proposal:
    """A proposal ``by`` requested, approved and later rejected, as the use cases store it."""
    proposal = _proposal(mower_id, account_id, requested_by=by, state=ProposalState.REJECTED)
    proposal.created_at = T0 + timedelta(minutes=1)
    await store.add_proposal(proposal, [])
    changes = {"approved_by": by, "approved_at": T0, "decided_by": by, "decided_at": T0}
    assert await store.update_proposal(proposal.id, changes)
    return proposal


async def seeded(
    store: Store, account_id: UUID, name: str = "Anna"
) -> tuple[UUID, UUID, UUID, Proposal]:
    """Every table of a user's own rows (the consent included) and a proposal with the user's
    name in each reference; returns the user, the mower, the seeded proposal and the user's."""
    user_id, mower_id, proposal_id = await seed(store, account_id)
    mine = await decided(store, account_id, mower_id, ref(user_id, name))
    return user_id, mower_id, proposal_id, mine


async def bens_own(
    store: Store, account_id: UUID, user_id: UUID, mower_id: UUID, proposal_id: UUID
) -> dict[str, object]:
    """What the store hands out about another member of the same account, without what Anna's
    erasure changes by design: the account's count of cost rows and the proposal she approved
    (both checked on their own)."""
    found = dict(await holdings(store, account_id, user_id, mower_id, proposal_id))
    del found["requests"], found["proposals"]
    return found


# --- the schema ------------------------------------------------------------------------------


def test_vision_keeps_these_rows_of_a_user_and_these_names() -> None:
    """The erasure deletes every table with a ``user_id`` column and scrubs every column that
    stores a principal reference, both read from the schema. This fails when one is added or
    removed, so the README's list stays true."""
    assert {table.name for table in user_tables(metadata)} == OWN_ROWS
    assert {f"{c.table.name}.{c.name}" for c in principal_ref_columns(metadata)} == NAMES


# --- the event -------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
async def test_a_deleted_users_rows_and_name_go_and_the_proposals_stay(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    mine, other = uuid4(), uuid4()
    async with store_of(kind, monkeypatch) as store, harness(store=store) as h:  # type: ignore[arg-type]
        anna, annas_mower, annas_seed, annas = await seeded(store, mine)
        ben, bens_mower, bens_seed, bens = await seeded(store, mine, "Ben")
        carl, carls_mower, carls_seed, _ = await seeded(store, other, "Carl")
        # Ben's proposal approved by Anna: her name goes there too, Ben's stays.
        changes = {"approved_by": ref(anna, "Anna")}
        assert await store.update_proposal(bens.id, changes)
        before = await store.user_leftovers(anna)
        # The seed reaches every table of a user's own rows and every reference column.
        assert set(before) == OWN_ROWS | NAMES
        account_before = await store.account_rows(mine)
        kept = {
            "ben": await bens_own(store, mine, ben, bens_mower, bens_seed),
            "carl": await holdings(store, other, carl, carls_mower, carls_seed),
            "ben_left": await store.user_leftovers(ben),
            "carl_left": await store.user_leftovers(carl),
            "ben_consent": await store.get_consent(ben, mine),
            "other_account": await store.account_rows(other),
        }

        await deleted(h.bus, anna, mine)
        await deleted(h.bus, anna, mine)  # a redelivered event changes nothing

        after = await store.user_leftovers(anna)
        again = await store.forget_user(anna, mine)
        own = await holdings(store, mine, anna, annas_mower, annas_seed)
        consent = await store.get_consent(anna, mine)
        scrubbed = await store.get_proposal(annas.id)
        approved = await store.get_proposal(bens.id)
        account_after = await store.account_rows(mine)
        still = {
            "ben": await bens_own(store, mine, ben, bens_mower, bens_seed),
            "carl": await holdings(store, other, carl, carls_mower, carls_seed),
            "ben_left": await store.user_leftovers(ben),
            "carl_left": await store.user_leftovers(carl),
            "ben_consent": await store.get_consent(ben, mine),
            "other_account": await store.account_rows(other),
        }
    assert after == {} and again == 0
    assert own["provider"] is None and own["preferences"] is None and consent is None
    assert own["auto"] is None or not own["auto"].enabled
    # The proposals stay with the account, with the opaque id and without the name.
    assert scrubbed is not None and own["proposals"]
    for reference in (scrubbed.requested_by, scrubbed.approved_by, scrubbed.decided_by):
        assert reference == PrincipalRef(subject=anna, kind=PrincipalKind.SUBUSER)
    assert approved is not None and approved.approved_by is not None
    assert approved.approved_by.display_name is None
    assert approved.requested_by == ref(ben, "Ben")
    # Only her own rows went: one provider, one preferences row and one cost row.
    removed = {t: account_before[t] - account_after.get(t, 0) for t in account_before}
    assert {t: n for t, n in removed.items() if n} == {
        "provider_requests": 1,
        "user_preferences": 1,
        "user_providers": 1,
    }
    assert still == kept
    assert h.bus.dropped == []


@pytest.mark.parametrize("kind", KINDS)
async def test_the_account_of_the_event_bounds_the_erasure(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    mine, other = uuid4(), uuid4()
    async with store_of(kind, monkeypatch) as store:
        anna, _, _, _ = await seeded(store, mine)
        await store.put_consent(anna, other, Consent(T0, CLOUD))
        await decided(store, other, uuid4(), ref(anna, "Anna"))
        in_account = await store.forget_user(anna, mine)
        left = await store.user_leftovers(anna)
        everywhere = await store.forget_user(anna, None)
        none = await store.user_leftovers(anna)
    assert in_account == 3 + 3  # three own rows, three references
    assert left == {"user_preferences": 1} | dict.fromkeys(NAMES, 1)
    assert everywhere == 1 + 3 and none == {}


class FlakyStore(MemoryStore):
    """A store whose first erasure of a user changes nothing."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def forget_user(self, user_id: UUID, account_id: UUID | None) -> int:
        self.calls += 1
        if self.calls == 1:
            return 0
        return await super().forget_user(user_id, account_id)


async def test_what_remains_fails_the_event_and_a_redelivery_finishes() -> None:
    """An erasure that leaves a row or a name behind is never acknowledged as done."""
    store = FlakyStore()
    async with harness(store=store) as h:
        mine = uuid4()
        anna, _, _, _ = await seeded(store, mine)
        erasure = h.app.state.vision_user_erasure
        with pytest.raises(UserErasureIncomplete) as raised:
            await erasure.erase(anna, mine)
        assert set(raised.value.leftovers) == OWN_ROWS | NAMES
        store.calls = 0
        await deleted(h.bus, anna, mine)  # fails once, the bus delivers it again
        left = await store.user_leftovers(anna)
    assert store.calls == 2
    assert left == {} and h.bus.dropped == []


async def test_a_disabled_user_keeps_the_cost_rows_and_the_name() -> None:
    """``identity.user.disabled`` stops the user's own model and opt-ins only: the user may be
    enabled again, so the cost rows (the daily caps) and the name on the proposals stay."""
    async with harness() as h:
        mine = uuid4()
        anna, _, _, _ = await seeded(h.store, mine)
        payload = UserChanged(user_id=anna, account_id=mine, reason="disabled")
        await h.bus.publish(
            event_subject_for_type("identity.user.disabled"),
            EventEnvelope[UserChanged](
                type="identity.user.disabled",
                version=2,
                producer="identity",
                account_id=mine,
                payload=payload,
            ),
        )
        left = await h.store.user_leftovers(anna)
    assert left == {"provider_requests": 1} | dict.fromkeys(NAMES, 1)


async def test_a_user_vision_never_saw_and_a_malformed_event_are_no_error() -> None:
    async with harness() as h:
        await deleted(h.bus, uuid4(), uuid4())
        await h.bus.publish(SUBJECT, b'{"type": "identity.user.deleted", "payload": {}}')
        drops = [m for m in h.bus.dropped if m.subject == SUBJECT]
    # Dropped once per consumer (the user erasure's and the identity events'), never retried.
    assert len(drops) == 2


def test_the_consumer_is_durable_and_shared_by_the_replicas() -> None:
    assert USERS_CONSUMER == "vision-users"
