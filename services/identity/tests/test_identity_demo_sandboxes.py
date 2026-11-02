"""Demo sandboxes (ADR 0076): every visitor of the public demo gets an own account.

Pool, claim, limits, idle and expiry sweep, the purge in every service (resumed after a failure)
and the isolation of two visitors, with a fake provisioner in place of the other services.
"""

import importlib.util
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from identity_support import API, Harness, bearer, harness
from mowdeck_authz import account, user
from mowdeck_service_kit.demo import DEMO_SEED_HEADER

from identity.adapters.demo_sandbox import MOWERS, PURGE_ORDER
from identity.application.sandboxes import SandboxService
from identity.domain.models import SandboxState

SEED = "demo-seed-secret"
REFRESH = {"X-Mowdeck-Refresh": "cookie-only", "X-Requested-With": "mowdeck-web"}
SANDBOXES: dict[str, Any] = {
    "demo_mode": True,
    "demo_seed_token": SEED,
    "identity_demo_sandboxes": True,
    "identity_demo_sandbox_pool": 0,
}


class FakeProvisioner:
    """Records what Identity asks the other services to do; fails on demand."""

    def __init__(self) -> None:
        self.provisioned: list[tuple[UUID, bool]] = []
        self.purged: list[tuple[str, UUID]] = []
        self.fail_provision = False
        self.fail_purge: dict[str, int] = {}

    @property
    def services(self) -> tuple[str, ...]:
        return PURGE_ORDER

    async def ready(self) -> bool:
        return True

    async def provision(self, account_id: UUID, access_token: str, *, history: bool) -> None:
        assert access_token  # the sandbox user's own token
        if self.fail_provision:
            msg = "fleet did not answer"
            raise RuntimeError(msg)
        self.provisioned.append((account_id, history))

    async def purge(self, service: str, account_id: UUID) -> None:
        if self.fail_purge.get(service, 0) > 0:
            self.fail_purge[service] -= 1
            msg = f"{service} is down"
            raise RuntimeError(msg)
        self.purged.append((service, account_id))


def _sandboxes(h: Harness) -> SandboxService:
    service: SandboxService | None = h.app.state.identity.sandboxes
    assert service is not None
    return service


async def _enter(h: Harness) -> dict[str, Any]:
    """A new visitor (no cookies) clicks "Try the demo"; returns the token answer and /me."""
    h.client.cookies.clear()
    response = await h.client.post(f"{API}/demo/session", headers=REFRESH)
    assert response.status_code == 200, response.text
    token = response.json()["access_token"]
    me = await h.client.get(f"{API}/me", headers=bearer(token))
    assert me.status_code == 200, me.text
    return {"token": token, "me": me.json(), "cookies": dict(h.client.cookies)}


async def _rows(h: Harness) -> list[Any]:
    async with h.store.transaction() as tx:
        return await tx.sandboxes.find()


async def test_every_visitor_gets_an_own_account() -> None:
    fake = FakeProvisioner()
    async with harness(provisioner=fake, **SANDBOXES) as h:
        anna = await _enter(h)
        ben = await _enter(h)
        rows = await _rows(h)
        other = await h.client.get(
            f"{API}/accounts/{ben['me']['account_id']}", headers=bearer(anna["token"])
        )
    assert anna["me"]["account_id"] != ben["me"]["account_id"]
    assert anna["me"]["subject"] != ben["me"]["subject"]
    assert anna["me"]["username"].startswith("visitor-")
    assert {r.state for r in rows} == {SandboxState.ACTIVE}
    # Made on the spot without history (the pool was empty), as the visitor's own user.
    assert [h for _, h in fake.provisioned] == [False, False]
    assert other.status_code == 404  # another visitor's account does not exist for Anna


async def test_a_ready_pool_sandbox_is_claimed_with_its_history() -> None:
    fake = FakeProvisioner()
    async with harness(provisioner=fake, **{**SANDBOXES, "identity_demo_sandbox_pool": 2}) as h:
        made = await _sandboxes(h).fill_pool()
        pooled = {r.account_id for r in await _rows(h)}
        visitor = await _enter(h)
        again = await _sandboxes(h).fill_pool()
        rows = {r.account_id: r.state for r in await _rows(h)}
    assert made == 2
    assert UUID(visitor["me"]["account_id"]) in pooled
    assert rows[UUID(visitor["me"]["account_id"])] is SandboxState.ACTIVE
    assert again == 1  # the claim asked for a refill
    assert sum(1 for s in rows.values() if s is SandboxState.READY) == 2
    assert all(history for _, history in fake.provisioned)


async def test_the_onboarding_of_a_sandbox_is_done() -> None:
    async with harness(provisioner=FakeProvisioner(), **SANDBOXES) as h:
        visitor = await _enter(h)
        onboarding = await h.client.get(f"{API}/me/onboarding", headers=bearer(visitor["token"]))
    assert onboarding.json()["status"] == "completed"


async def test_a_full_demo_answers_503_with_retry_after() -> None:
    settings = {**SANDBOXES, "identity_demo_sandbox_max": 2}
    async with harness(provisioner=FakeProvisioner(), **settings) as h:
        await _enter(h)
        await _enter(h)
        h.client.cookies.clear()
        full = await h.client.post(f"{API}/demo/session", headers=REFRESH)
    assert full.status_code == 503
    assert full.json()["type"] == "urn:mowdeck:problem:demo-full"
    assert int(full.headers["Retry-After"]) > 0


async def test_one_address_may_start_only_a_few_sandboxes_an_hour() -> None:
    settings = {**SANDBOXES, "identity_demo_sandbox_per_address_per_hour": 2}
    async with harness(provisioner=FakeProvisioner(), **settings) as h:
        await _enter(h)
        await _enter(h)
        h.client.cookies.clear()
        third = await h.client.post(f"{API}/demo/session", headers=REFRESH)
    assert third.status_code == 429
    assert int(third.headers["Retry-After"]) > 0


async def test_a_sandbox_that_cannot_be_prepared_is_given_up() -> None:
    fake = FakeProvisioner()
    fake.fail_provision = True
    async with harness(provisioner=fake, **SANDBOXES) as h:
        h.client.cookies.clear()
        response = await h.client.post(f"{API}/demo/session", headers=REFRESH)
        rows = await _rows(h)
    assert response.status_code == 503
    assert [(r.state, r.purge_reason) for r in rows] == [
        (SandboxState.PURGING, "provisioning failed")
    ]


async def _purge_everything(h: Harness) -> None:
    """Sweep, wait out the settle time after Fleet, sweep again."""
    await _sandboxes(h).sweep()
    h.clock.advance(timedelta(seconds=31))
    await _sandboxes(h).sweep()


async def test_an_idle_sandbox_is_deleted_everywhere() -> None:
    fake = FakeProvisioner()
    async with harness(provisioner=fake, **SANDBOXES) as h:
        visitor = await _enter(h)
        stays = await _enter(h)
        account_id = UUID(visitor["me"]["account_id"])
        user_id = UUID(visitor["me"]["subject"])
        h.clock.advance(timedelta(minutes=29))
        # The second visitor's tab refreshes; the first one closed theirs.
        h.client.cookies.clear()
        h.client.cookies.update(stays["cookies"])
        refreshed = await h.client.post(f"{API}/session/refresh", headers=REFRESH)
        assert refreshed.status_code == 200, refreshed.text
        h.clock.advance(timedelta(minutes=2))
        first = await _sandboxes(h).sweep()
        after_fleet = [s for s, a in fake.purged if a == account_id]
        h.clock.advance(timedelta(seconds=31))
        second = await _sandboxes(h).sweep()
        rows = {r.account_id: r for r in await _rows(h)}
        async with h.store.transaction() as tx:
            left = (
                await tx.accounts.get(id=account_id),
                await tx.users.get(id=user_id),
                await tx.memberships.find(account_id=account_id),
                await tx.roles.find(account_id=account_id),
                await tx.sessions.find(user_id=user_id),
                await tx.audit.find(account_id=account_id),
                await tx.onboarding.find(user_id=user_id),
            )
        tuples = await h.authz.read(account(account_id))
        user_tuples = await h.authz.read("account", subject=user(user_id))
        h.client.cookies.clear()
        h.client.cookies.update(visitor["cookies"])
        dead = await h.client.post(f"{API}/session/refresh", headers=REFRESH)
    assert first.marked == 1
    assert after_fleet == ["fleet"]  # the others wait until the mowers' last states landed
    assert second.purged == 1
    assert [s for s, a in fake.purged if a == account_id] == list(PURGE_ORDER)
    assert account_id not in rows
    assert UUID(stays["me"]["account_id"]) in rows
    assert left == (None, None, [], [], [], [], [])
    assert tuples == []
    assert user_tuples == []
    assert dead.status_code == 401


async def test_a_logout_ends_the_sandbox() -> None:
    fake = FakeProvisioner()
    async with harness(provisioner=fake, **SANDBOXES) as h:
        visitor = await _enter(h)
        logout = await h.client.post(f"{API}/session/logout", headers=REFRESH)
        result = await _sandboxes(h).sweep()
        rows = await _rows(h)
    assert logout.status_code in (200, 204)
    assert result.marked == 1
    assert rows[0].purge_reason == "ended"
    assert rows[0].account_id == UUID(visitor["me"]["account_id"])


async def test_a_sandbox_ends_after_the_maximum_age_however_active() -> None:
    async with harness(provisioner=FakeProvisioner(), **SANDBOXES) as h:
        await _enter(h)
        for _ in range(13):  # a refresh every 30 minutes for 6.5 hours
            h.clock.advance(timedelta(minutes=30))
            refreshed = await h.client.post(f"{API}/session/refresh", headers=REFRESH)
            if refreshed.status_code != 200:
                break
        result = await _sandboxes(h).sweep()
        rows = await _rows(h)
    assert result.marked == 1
    assert rows[0].purge_reason == "expired"


async def test_a_failed_purge_is_retried_and_resumes_where_it_stopped() -> None:
    fake = FakeProvisioner()
    fake.fail_purge = {"telemetry": 2}
    async with harness(provisioner=fake, **SANDBOXES) as h:
        visitor = await _enter(h)
        account_id = UUID(visitor["me"]["account_id"])
        await h.client.post(f"{API}/session/logout", headers=REFRESH)
        await _purge_everything(h)  # telemetry fails once
        row = (await _rows(h))[0]
        assert row.state is SandboxState.PURGING
        assert row.purged == ["fleet", "scheduler"]
        assert row.last_error is not None and "telemetry" in row.last_error
        h.clock.advance(timedelta(seconds=10))
        early = await _sandboxes(h).sweep()  # backoff: not yet
        h.clock.advance(timedelta(minutes=10))
        await _sandboxes(h).sweep()  # fails a second time
        h.clock.advance(timedelta(minutes=10))
        done = await _sandboxes(h).sweep()
        rows = await _rows(h)
    assert early.purged == 0
    assert done.purged == 1
    assert rows == []
    calls = [s for s, a in fake.purged if a == account_id]
    assert calls == list(PURGE_ORDER)  # every service once, none twice


async def test_a_sandbox_stuck_in_provisioning_is_given_up() -> None:
    async with harness(provisioner=FakeProvisioner(), **SANDBOXES) as h:
        sandbox = await _sandboxes(h)._create(claimed_at=None)
        h.clock.advance(timedelta(minutes=16))
        result = await _sandboxes(h).sweep()
        rows = await _rows(h)
    assert result.marked == 1
    assert rows == [] or rows[0].purge_reason == "stuck"
    assert sandbox.state is SandboxState.PROVISIONING


async def test_sandboxes_need_demo_mode() -> None:
    settings = {**SANDBOXES, "demo_mode": False}
    async with harness(provisioner=FakeProvisioner(), **settings) as h:
        response = await h.client.post(f"{API}/demo/session", headers=REFRESH)
        assert h.app.state.identity.sandboxes is None
    assert response.status_code == 404


async def test_without_sandboxes_the_shared_demo_user_stays() -> None:
    settings = {**SANDBOXES, "identity_demo_sandboxes": False}
    async with harness(provisioner=FakeProvisioner(), **settings) as h:
        await h.client.post(f"{API}/demo/seed", headers={DEMO_SEED_HEADER: SEED})
        response = await h.client.post(f"{API}/demo/session", headers=REFRESH)
        me = await h.client.get(f"{API}/me", headers=bearer(response.json()["access_token"]))
    assert me.json()["username"] == "demo"


@pytest.mark.parametrize("field", ["name", "connection_options", "offset_m", "schedule"])
def test_the_sandbox_garden_is_the_shared_demo_garden(field: str) -> None:
    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location("demo_seed", root / "infra/compose/demo/seed.py")
    assert spec is not None and spec.loader is not None
    seed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed)
    assert [m[field] for m in MOWERS] == [m[field] for m in seed.MOWERS]
