# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Account erasure (ADR 0082): ``account.erase.vision`` removes everything Vision holds of a
deleted account, outside the demo, and reports honestly when it could not.

The requests go over the in-memory bus to the real app, as Identity sends them. The store is
the memory store and, with ``MOWDECK_TEST_DATABASE_URL``, PostgreSQL (see ``test_vision_db``).
"""

import base64
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from mowdeck_contracts.v1 import (
    AccountEraseReply,
    AccountEraseRequest,
    CommandEnvelope,
    PrincipalKind,
    PrincipalRef,
)
from mowdeck_contracts.v1.subjects import account_erase_subject
from mowdeck_control import flat_png
from mowdeck_service_kit.db import account_tables
from mowdeck_service_kit.erasure import ErasureIncomplete
from test_vision_db import _proposal, needs_db
from test_vision_demo_purge import fill, holdings, store_of
from vision_support import API, T0, Harness, Person, SimRig, StaticFactory, harness, wait_for

from vision.adapters.bus.view import BusMowerView
from vision.adapters.db.tables import metadata
from vision.adapters.memory import MemoryStore
from vision.application.ports import Store
from vision.domain.consent import Consent, Destination, DestinationKind
from vision.domain.models import ProviderRequest

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
KINDS = ["memory", pytest.param("sql", marks=needs_db)]

PROPOSAL_TABLES = {"proposal_images", "proposal_steps"}
"""The tables without an ``account_id`` column: the pictures the model saw and the drive steps
belong to a proposal (foreign key, ``ON DELETE CASCADE``) and are counted through it."""

CLOUD = Destination(DestinationKind.CLOUD, "Anthropic", "anthropic", "anthropic/claude-sonnet-5-5")


async def erase(h: Harness, account_id: UUID) -> AccountEraseReply:
    """What Identity's ``BusAccountEraser`` sends, and the decoded reply."""
    request = AccountEraseRequest(account_id=account_id, requested_at=NOW)
    message = await h.bus.request(account_erase_subject("vision"), request, timeout=10.0)
    reply: AccountEraseReply = message.model(AccountEraseReply)
    return reply


async def seed(store: Store, account_id: UUID) -> tuple[UUID, UUID, UUID]:
    """One row in every table of the account, the consent to a cloud model included; returns
    the user, the mower and the proposal."""
    user_id, mower_id, proposal_id = await fill(store, account_id)
    await store.put_consent(user_id, account_id, Consent(T0, CLOUD))
    return user_id, mower_id, proposal_id


def a_request(account_id: UUID, proposal_id: UUID | None) -> ProviderRequest:
    return ProviderRequest(
        id=uuid4(),
        account_id=account_id,
        user_id=uuid4(),
        kind="analysis",
        model="fake/recover",
        family="fake",
        hosted=True,
        outcome="ok",
        created_at=T0,
        proposal_id=proposal_id,
    )


async def analysing(h: Harness, owner: Person) -> tuple[SimRig, str]:
    """A mower of ``owner`` with a requested analysis (the model has the request)."""
    rig = await h.rig(owner)
    picture = base64.b64encode(flat_png(8, 6, (1, 2, 3))).decode()
    response = await h.client.post(
        f"{API}/mowers/{rig.mower_id}/recover",
        json={"task": "recover", "images": [{"media_type": "image/png", "data": picture}]},
        headers=owner.headers,
    )
    assert response.status_code == 202, response.text
    proposal_id: str = response.json()["id"]
    return rig, proposal_id


async def removed_by_fleet(h: Harness, rig: SimRig) -> None:
    """Fleet is asked first and removes the mower: its gateway stops reporting."""
    h.rigs.remove(rig)
    await rig.stop()


# --- the schema ------------------------------------------------------------------------------


def test_every_table_of_vision_belongs_to_an_account_or_to_a_proposal() -> None:
    """The erasure counts the tables with an ``account_id`` column and the two that hang on a
    proposal. Another table without the column would be invisible to it: this fails when one
    is added, so its rows get an owner or a decision."""
    without = {table.name for table in metadata.sorted_tables} - {
        table.name for table in account_tables(metadata)
    }
    assert without == PROPOSAL_TABLES
    for name in without:
        parents = {
            (key.column.table.name, key.ondelete) for key in metadata.tables[name].foreign_keys
        }
        assert parents == {("proposals", "CASCADE")}
    assert "account_id" in metadata.tables["proposals"].c


def test_no_table_of_vision_is_keyed_by_the_user_alone() -> None:
    """Every table with a ``user_id`` (providers, preferences with the consent, provider
    requests) also names the account."""
    by_user = {table.name for table in metadata.sorted_tables if "user_id" in table.c}
    assert by_user and by_user <= {table.name for table in account_tables(metadata)}


# --- the request -----------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
async def test_erasure_removes_every_row_of_one_account_and_keeps_the_other(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    gone, kept = uuid4(), uuid4()
    async with store_of(kind, monkeypatch) as store, harness(store=store) as h:  # type: ignore[arg-type]  # not a demo
        gone_rows = await seed(store, gone)
        kept_rows = await seed(store, kept)
        before = await store.account_rows(gone)
        kept_counts = await store.account_rows(kept)
        kept_holdings = await holdings(store, kept, *kept_rows)
        # The seed reaches every table of the schema, so none is checked empty.
        assert set(before) == set(metadata.tables)
        assert before["proposal_images"] == 1 and before["proposal_steps"] == 2

        first = await erase(h, gone)
        second = await erase(h, gone)
        unknown = await erase(h, uuid4())

        assert first == AccountEraseReply(ok=True, deleted=sum(before.values()))
        assert second == AccountEraseReply(ok=True, deleted=0)
        assert unknown == AccountEraseReply(ok=True, deleted=0)
        assert await store.account_rows(gone) == {}
        found = await holdings(store, gone, *gone_rows)
        assert all(not value for value in found.values()), found
        assert await store.get_image(gone_rows[2], 0) is None  # the picture, not only its row
        assert await store.get_consent(gone_rows[0], gone) is None
        assert await store.account_rows(kept) == kept_counts
        assert await holdings(store, kept, *kept_rows) == kept_holdings
        assert await store.get_consent(kept_rows[0], kept) == Consent(T0, CLOUD)


async def test_erasure_reports_rows_that_remain_and_a_retry_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A purge that leaves rows behind is never answered as a success: the pictures are named
    in the reply like every other table."""

    async def purge_nothing(self: MemoryStore, account_id: UUID) -> int:
        del self, account_id
        return 0

    account_id = uuid4()
    async with harness() as h:
        await seed(h.store, account_id)
        left = await h.store.account_rows(account_id)
        with monkeypatch.context() as patched:
            patched.setattr(MemoryStore, "purge_account", purge_nothing)
            failed = await erase(h, account_id)
        assert await h.store.account_rows(account_id) == left
        retried = await erase(h, account_id)

        assert failed == AccountEraseReply(ok=False, deleted=0, error=str(ErasureIncomplete(left)))
        assert "proposal_images: 1" in (failed.error or "")
        assert retried == AccountEraseReply(ok=True, deleted=sum(left.values()))
        assert await h.store.account_rows(account_id) == {}


async def test_erasure_reports_pictures_that_remain(monkeypatch: pytest.MonkeyPatch) -> None:
    """The count reaches the tables without an account column: a purge that removed everything
    but the proposal and its picture is a failure that names both."""
    account_id = uuid4()
    async with harness() as h:
        _, _, proposal_id = await seed(h.store, account_id)
        proposal = h.store.proposals[proposal_id]
        picture = h.store.images[(proposal_id, 0)]

        async def purge_but_the_picture(self: MemoryStore, account: UUID) -> int:
            del account
            removed = self._rows()
            MemoryStore.__init__(self)
            self.proposals[proposal_id] = proposal
            self.images[(proposal_id, 0)] = picture
            return removed - 2

        monkeypatch.setattr(MemoryStore, "purge_account", purge_but_the_picture)
        failed = await erase(h, account_id)

        assert failed.ok is False
        assert failed.error == "rows of the account remain (proposal_images: 1, proposals: 1)"


async def test_erasure_reports_a_failing_purge(monkeypatch: pytest.MonkeyPatch) -> None:
    async def purge_fails(self: MemoryStore, account_id: UUID) -> int:
        del self, account_id
        raise ConnectionError("database is down")

    account_id = uuid4()
    async with harness() as h:
        await seed(h.store, account_id)
        monkeypatch.setattr(MemoryStore, "purge_account", purge_fails)

        failed = await erase(h, account_id)

        assert failed == AccountEraseReply(
            ok=False, deleted=0, error="ConnectionError: see the log of vision"
        )
        assert await h.store.account_rows(account_id) != {}


# --- what a real account has and a demo sandbox does not --------------------------------------


@pytest.mark.timeout(60)
async def test_erasure_stops_the_analysis_running_for_the_account() -> None:
    """An analysis in flight would record its result after the purge: it is stopped first, the
    other account's goes on, and nothing of the erased account comes back."""
    async with harness(factory=StaticFactory(delay=30.0)) as h:
        alice, bob = h.owner(), h.owner()
        alice_rig, _ = await analysing(h, alice)
        _, bob_proposal = await analysing(h, bob)
        await wait_for(lambda: len(h.factory.calls) == 2)
        await removed_by_fleet(h, alice_rig)
        before = await h.store.account_rows(alice.account_id)
        assert before["proposals"] == 1 and before["proposal_images"] == 1
        running = h.service._task_accounts

        reply = await erase(h, alice.account_id)

        assert reply.ok and reply.error is None
        # The rows that were there plus the request row the interrupted analysis recorded.
        assert reply.deleted == sum(before.values()) + 1
        assert await h.store.account_rows(alice.account_id) == {}
        assert list(running.values()) == [bob.account_id]
        assert len(h.factory.calls) == 2  # nothing was sent to a model by the erasure
        assert (await h.proposal(bob_proposal, bob))["state"] == "analysing"
        assert h.service.view.state(alice_rig.mower_id) is None


@pytest.mark.timeout(60)
async def test_an_analysis_that_outlives_its_proposal_stores_nothing() -> None:
    """Another replica erased the account while this one waited for the model: the answer is
    dropped instead of becoming a request row of the erased account."""
    async with harness(factory=StaticFactory(delay=0.3)) as h:
        owner = h.owner()
        rig, _ = await analysing(h, owner)
        await wait_for(lambda: len(h.factory.calls) == 1)
        await removed_by_fleet(h, rig)

        assert await h.store.purge_account(owner.account_id) >= 2  # the other replica's purge
        await h.service.idle()

        assert await h.store.account_rows(owner.account_id) == {}


@pytest.mark.parametrize("kind", KINDS)
async def test_a_request_is_recorded_only_while_its_proposal_exists(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    account_id = uuid4()
    async with store_of(kind, monkeypatch) as store:
        proposal = _proposal(uuid4(), account_id)
        await store.add_proposal(proposal, [])

        assert await store.record_request(a_request(account_id, proposal.id)) is True
        assert await store.record_request(a_request(account_id, None)) is True  # a provider test
        assert await store.record_request(a_request(account_id, uuid4())) is False

        assert (await store.account_rows(account_id))["provider_requests"] == 2
        await store.purge_account(account_id)
        assert await store.record_request(a_request(account_id, proposal.id)) is False
        assert await store.account_rows(account_id) == {}


def test_the_view_forgets_a_mower_it_only_saw_commands_of() -> None:
    """Commands name the account too: a mower without a state or snapshot is still found."""
    view = BusMowerView()
    issuer = PrincipalRef(subject=uuid4(), kind=PrincipalKind.USER)
    mowers = {uuid4(): uuid4(), uuid4(): uuid4()}  # mower -> account
    for mower_id, account_id in mowers.items():
        view.on_command(
            CommandEnvelope.create(
                mower_id=mower_id,
                account_id=account_id,
                issued_by=issuer,
                command={"kind": "start_mow", "area_ids": ["1"]},  # type: ignore[arg-type]
                ttl=timedelta(seconds=30),
            )
        )
    (gone, gone_account), (kept, _) = mowers.items()

    assert view.forget_account(gone_account) == {gone}
    assert view.recent_commands(gone) == [] and view.last_start(gone) is None
    assert len(view.recent_commands(kept)) == 1 and view.last_start(kept) == ["1"]
    assert view.forget_account(gone_account) == set()
