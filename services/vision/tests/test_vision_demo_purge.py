"""Demo sandboxes (ADR 0076): the purge of one expired visitor account.

The API tests run the real app on the memory store; the store test runs on the memory store
and, with ``MOWDECK_TEST_DATABASE_URL``, on PostgreSQL (see ``test_vision_db``).
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from mowdeck_contracts.v1 import ConnectionKind, MowerActivity, MowerState
from mowdeck_service_kit.db import async_database_url
from mowdeck_service_kit.demo import DEMO_SEED_HEADER
from sqlalchemy.ext.asyncio import create_async_engine
from test_vision_db import _proposal, migrate, needs_db, temporary_database
from vision_support import API, T0, harness

from vision.adapters.bus.view import BusMowerView
from vision.adapters.db.store import SqlStore
from vision.adapters.memory import MemoryStore
from vision.application.auto import AutoAnalysis
from vision.application.ports import Store
from vision.domain.models import (
    AutoSetting,
    ProposalState,
    ProposalStep,
    ProviderRequest,
    StoredImage,
    UserPreferences,
    UserProvider,
)

SEED = "demo-seed-secret"
DEMO: dict[str, Any] = {"demo_mode": True, "demo_seed_token": SEED}
ROWS = 8
"""What :func:`fill` writes: proposal, image, two steps, provider request, provider, one
preferences row (preferences and opt-in) and the error watch."""


def purge_path(account_id: UUID) -> str:
    return f"{API}/demo/accounts/{account_id}"


async def fill(store: Store, account_id: UUID) -> tuple[UUID, UUID, UUID]:
    """One row in every table of the account; returns the user, the mower and the proposal."""
    user_id, mower_id = uuid4(), uuid4()
    proposal = _proposal(mower_id, account_id, state=ProposalState.REJECTED)
    image = StoredImage(
        proposal.id, 0, "image/jpeg", b"\xff\xd8jpeg", "mower", T0 + timedelta(days=1)
    )
    await store.add_proposal(proposal, [image])
    await store.put_steps([ProposalStep(proposal.id, i, -0.5, 0.0, 1.0) for i in range(2)])
    await store.record_request(
        ProviderRequest(
            id=uuid4(),
            account_id=account_id,
            user_id=user_id,
            kind="analysis",
            model="fake/recover",
            family="fake",
            hosted=True,
            outcome="ok",
            created_at=T0,
            proposal_id=proposal.id,
        )
    )
    await store.put_provider(UserProvider(user_id, account_id, "fake/recover", True, T0, T0))
    await store.put_preferences(UserPreferences(user_id, account_id, False, T0))
    await store.put_auto_setting(AutoSetting(user_id, account_id, True, T0))
    assert await store.swap_auto_watch(mower_id, account_id, "1201", T0) is None
    return user_id, mower_id, proposal.id


async def holdings(
    store: Store, account_id: UUID, user_id: UUID, mower_id: UUID, proposal_id: UUID
) -> dict[str, Any]:
    """What the store hands out about the account (read only)."""
    return {
        "proposals": await store.list_proposals(account_id, mower_id, limit=10),
        "image": await store.get_image(proposal_id, 0),
        "steps": await store.steps(proposal_id),
        "requests": await store.count_requests(since=T0, kind="analysis", account_id=account_id),
        "provider": await store.get_provider(user_id, account_id),
        "preferences": await store.get_preferences(user_id, account_id),
        "auto": await store.get_auto_setting(user_id, account_id),
        "counted": await store.count_proposals(mower_id, since=T0),
    }


# --- the endpoint --------------------------------------------------------------------------


async def test_purge_is_a_demo_only_seed_endpoint() -> None:
    account_id = uuid4()
    async with harness(demo_seed_token=SEED) as h:
        outside = await h.client.delete(purge_path(account_id), headers={DEMO_SEED_HEADER: SEED})
    async with harness(**DEMO) as h:
        without = await h.client.delete(purge_path(account_id))
        wrong = await h.client.delete(purge_path(account_id), headers={DEMO_SEED_HEADER: "nope"})
    assert outside.status_code == 404
    assert without.status_code == 403
    assert wrong.status_code == 403


def a_state(mower_id: UUID, account_id: UUID) -> MowerState:
    return MowerState(
        mower_id=mower_id,
        account_id=account_id,
        observed_at=T0,
        activity=MowerActivity.IDLE,
        battery_percent=80,
        connection=ConnectionKind.SIMULATED,
    )


async def test_purge_removes_one_account_and_keeps_the_other() -> None:
    async with harness(**DEMO) as h:
        gone, kept = uuid4(), uuid4()
        gone_rows = await fill(h.store, gone)
        kept_rows = await fill(h.store, kept)
        view = h.service.view
        assert isinstance(view, BusMowerView)
        auto: AutoAnalysis = h.app.state.auto_analysis
        for account_id, (_, mower_id, _) in ((gone, gone_rows), (kept, kept_rows)):
            view.on_state(a_state(mower_id, account_id))
            auto._known[mower_id] = ("1201", 0.0)
        before = await holdings(h.store, kept, *kept_rows)

        first = await h.client.delete(purge_path(gone), headers={DEMO_SEED_HEADER: SEED})
        second = await h.client.delete(purge_path(gone), headers={DEMO_SEED_HEADER: SEED})
        nobody = await h.client.delete(purge_path(uuid4()), headers={DEMO_SEED_HEADER: SEED})

        assert first.status_code == 200, first.text
        assert first.json() == {"deleted": ROWS}
        assert second.status_code == 200
        assert second.json() == {"deleted": 0}
        assert nobody.json() == {"deleted": 0}
        found = await holdings(h.store, gone, *gone_rows)
        assert all(not value for value in found.values()), found
        assert gone_rows[1] not in h.store.watch
        assert view.state(gone_rows[1]) is None
        assert gone_rows[1] not in auto._known
        assert await holdings(h.store, kept, *kept_rows) == before
        assert h.store.watch[kept_rows[1]] == "1201"
        assert view.state(kept_rows[1]) is not None
        assert kept_rows[1] in auto._known


# --- the store (memory and PostgreSQL) -----------------------------------------------------


@asynccontextmanager
async def store_of(kind: str, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Store]:
    if kind == "memory":
        yield MemoryStore()
        return
    async with temporary_database() as url:
        await migrate(url, monkeypatch)
        engine = create_async_engine(async_database_url(url))
        try:
            yield SqlStore(engine)
        finally:
            await engine.dispose()


@pytest.mark.parametrize("kind", ["memory", pytest.param("sql", marks=needs_db)])
async def test_store_purge_deletes_every_table_of_one_account(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with store_of(kind, monkeypatch) as store:
        gone, kept = uuid4(), uuid4()
        gone_rows = await fill(store, gone)
        kept_rows = await fill(store, kept)
        before = await holdings(store, kept, *kept_rows)
        assert all(before.values())

        assert await store.purge_account(gone) == ROWS

        found = await holdings(store, gone, *gone_rows)
        assert all(not value for value in found.values()), found
        assert await holdings(store, kept, *kept_rows) == before
        # The error watch went too: the mower is new again; the other account's is kept.
        assert await store.swap_auto_watch(gone_rows[1], gone, None, T0) is None
        assert await store.swap_auto_watch(kept_rows[1], kept, "1201", T0) == "1201"
        assert await store.purge_account(gone) == 1  # the watch row the check wrote again
        assert await store.purge_account(gone) == 0
        assert await store.purge_account(uuid4()) == 0
