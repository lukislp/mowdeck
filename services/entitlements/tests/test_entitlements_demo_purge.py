"""Demo sandboxes (ADR 0076): the purge of one expired visitor account.

The API tests run the real app on the memory store; the store test runs on the memory store
and, with ``MOWDECK_TEST_DATABASE_URL``, on PostgreSQL (see ``test_entitlements_db``).
"""

from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from entitlements_support import API, T0, harness
from mowdeck_contracts.v1 import PROBLEM_TYPE_PREFIX, PlanSource, PlanStatus
from mowdeck_service_kit.demo import DEMO_PROBLEM_TYPE, DEMO_SEED_HEADER
from test_entitlements_db import STORES, _plan, store_of

from entitlements.adapters.api.demo import DEMO_POLICY
from entitlements.application.ports import Store
from entitlements.domain.models import AccountPlan, PlanHistory

SEED = "demo-seed-secret"
DEMO: dict[str, Any] = {"demo_mode": True, "demo_seed_token": SEED}


def purge_path(account_id: UUID) -> str:
    return f"{API}/demo/accounts/{account_id}"


# --- the endpoint --------------------------------------------------------------------------


async def test_purge_is_a_demo_only_seed_endpoint() -> None:
    account_id = uuid4()
    async with harness(demo_seed_token=SEED) as h:
        outside = await h.client.delete(purge_path(account_id), headers={DEMO_SEED_HEADER: SEED})
    async with harness(**DEMO) as h:
        without = await h.client.delete(purge_path(account_id))
        wrong = await h.client.delete(purge_path(account_id), headers={DEMO_SEED_HEADER: "nope"})
        allowed = await h.client.delete(purge_path(account_id), headers={DEMO_SEED_HEADER: SEED})
    assert outside.status_code == 404
    # The allow_only policy refuses it to everybody without the seed secret ...
    assert DEMO_POLICY.refusal("DELETE", purge_path(account_id))
    for refused in (without, wrong):
        assert refused.status_code == 403
        assert refused.json()["type"] == f"{PROBLEM_TYPE_PREFIX}{DEMO_PROBLEM_TYPE}"
    # ... and lets the seed job through.
    assert allowed.status_code == 200, allowed.text
    assert allowed.json() == {"deleted": 0}


async def test_purge_removes_one_account_and_keeps_the_other() -> None:
    async with harness(**DEMO) as h:
        gone, kept = uuid4(), uuid4()
        await h.account_created(gone)
        await h.account_created(kept)
        state = h.store.state
        assert {gone, kept} <= set(state.accounts)
        kept_detail = await h.services.entitlements.account(kept)
        plans = dict(state.plans)

        first = await h.client.delete(purge_path(gone), headers={DEMO_SEED_HEADER: SEED})
        second = await h.client.delete(purge_path(gone), headers={DEMO_SEED_HEADER: SEED})

        assert first.status_code == 200, first.text
        assert first.json()["deleted"] >= 1
        assert second.status_code == 200
        assert second.json() == {"deleted": 0}
        state = h.store.state
        assert gone not in state.accounts
        assert all(entry.account_id != gone for entry in state.history)
        assert kept in state.accounts
        assert await h.services.entitlements.account(kept) == kept_detail
        assert state.plans == plans  # the installation's plans stay


# --- the store (memory and PostgreSQL) -----------------------------------------------------


async def fill(store: Store, account_id: UUID) -> None:
    """The account's plan row and two history entries."""
    async with store.transaction() as tx:
        if await tx.get_plan("home") is None:
            await tx.save_plan(_plan("home", is_default=True))
        await tx.save_account(
            AccountPlan(
                account_id=account_id,
                plan_key="home",
                status=PlanStatus.ACTIVE,
                since=T0,
                source=PlanSource.MANUAL,
                updated_at=T0,
            )
        )
        for offset in range(2):
            await tx.add_history(
                PlanHistory(
                    id=uuid4(),
                    account_id=account_id,
                    at=T0 + timedelta(hours=offset),
                    plan_key="home",
                    status=PlanStatus.ACTIVE,
                    source=PlanSource.MANUAL,
                )
            )


@pytest.mark.parametrize("kind", STORES)
async def test_store_purge_deletes_the_rows_of_one_account(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with store_of(kind, monkeypatch) as store:
        gone, kept = uuid4(), uuid4()
        await fill(store, gone)
        await fill(store, kept)
        async with store.transaction() as tx:
            before = (await tx.get_account(kept), await tx.history(kept, 10))
            assert await tx.purge_account(gone) == 3
        async with store.transaction() as tx:
            assert await tx.get_account(gone) is None
            assert await tx.history(gone, 10) == []
            assert (await tx.get_account(kept), await tx.history(kept, 10)) == before
            assert await tx.get_plan("home") is not None
            assert await tx.purge_account(gone) == 0
            assert await tx.purge_account(uuid4()) == 0
