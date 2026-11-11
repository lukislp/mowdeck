# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Account erasure (ADR 0082): the purge after the grace period, with a fake in place of the
other services. Begin, ask in order, settle after Fleet, retry with backoff, finish, the audit
scrub, restore only before the purge, no purge in the demo, the operator's view."""

import asyncio
import io
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import pytest
from identity_support import API, PASSWORD, Account, Harness, bearer, harness, make_settings
from mowdeck_authz import account, user
from mowdeck_contracts.v1 import AccountEraseReply, AccountEraseRequest
from mowdeck_service_kit.bus import InMemoryBus

from identity.adapters.bus.erasure import BusAccountEraser
from identity.application.erasure import ErasureService
from identity.cli import amain
from identity.domain.erasure import AccountErasure, ErasureState
from identity.domain.models import AccountStatus
from identity.runtime import Runtime

DEMO: dict[str, Any] = {"demo_seed_token": "seed", "identity_erasure_enabled": True}
NOW_GRACE: dict[str, Any] = {
    "identity_erasure_enabled": True,
    "identity_deletion_grace_days": 0,
    "identity_erasure_services": "fleet,scheduler,notifications",
    "identity_erasure_settle_seconds": 30,
}


class FakeEraser:
    """Records what Identity asks the services; fails on demand."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, UUID]] = []
        self.fail: dict[str, int] = {}
        self.deleted = 7

    async def erase(self, service: str, account_id: UUID) -> int:
        if self.fail.get(service, 0) > 0:
            self.fail[service] -= 1
            msg = f"{service} is down"
            raise RuntimeError(msg)
        self.calls.append((service, account_id))
        return self.deleted


def _erasure(h: Harness) -> ErasureService:
    service: ErasureService | None = h.app.state.identity.erasure
    assert service is not None
    return service


async def _schedule(h: Harness, owner: Account) -> None:
    response = await h.client.request(
        "DELETE", owner.url(), headers=owner.headers, json={"password": PASSWORD}
    )
    assert response.status_code == 202, response.text


async def _record(h: Harness, account_id: UUID) -> AccountErasure:
    async with h.store.transaction() as tx:
        found = await tx.erasures.get(account_id=account_id)
    assert found is not None
    return found


EXPIRED = timedelta(minutes=11)
"""The access token lifetime plus the skew: the purge asks no service before the last token
issued for the account has run out (``test_identity_revoked_access`` has the rule's tests)."""


async def _begin(h: Harness) -> int:
    """Start the purges that are due and let the tokens of those accounts expire; how many."""
    begun = await _erasure(h).begin_due()
    h.clock.advance(EXPIRED)
    return begun


async def _finish(h: Harness) -> None:
    """Begin, sweep, wait out the settle time after Fleet, sweep again."""
    await _begin(h)
    await _erasure(h).sweep()
    h.clock.advance(timedelta(seconds=31))
    await _erasure(h).sweep()


async def test_nothing_happens_before_the_grace_period_is_over() -> None:
    fake = FakeEraser()
    async with harness(eraser=fake, **{**NOW_GRACE, "identity_deletion_grace_days": 7}) as h:
        lukas = await h.register("lukas")
        await _schedule(h, lukas)
        h.clock.advance(timedelta(days=6, hours=23))
        early = await _erasure(h).sweep()
        h.clock.advance(timedelta(hours=2))
        late = await _begin(h)
        await _erasure(h).sweep()
        async with h.store.transaction() as tx:
            acct = await tx.accounts.get(id=lukas.account_id)
    assert (early.begun, late) == (0, 1)
    assert acct is not None and acct.status is AccountStatus.PURGING
    assert fake.calls == [("fleet", lukas.account_id)]


async def test_a_deleted_account_is_erased_in_every_service_then_in_identity() -> None:
    fake = FakeEraser()
    async with harness(eraser=fake, **NOW_GRACE) as h:
        lukas = await h.register("lukas")
        bob = await h.register("bob")
        user_id = lukas.user_id
        await _schedule(h, lukas)
        await _begin(h)
        first = await _erasure(h).sweep()
        after_fleet = [s for s, _ in fake.calls]
        mid = await _record(h, lukas.account_id)
        h.clock.advance(timedelta(seconds=31))
        second = await _erasure(h).sweep()
        done = await _record(h, lukas.account_id)
        async with h.store.transaction() as tx:
            left = (
                await tx.accounts.get(id=lukas.account_id),
                await tx.users.get(id=user_id),
                await tx.memberships.find(account_id=lukas.account_id),
                await tx.roles.find(account_id=lukas.account_id),
                await tx.sessions.find(user_id=user_id),
                await tx.onboarding.find(user_id=user_id),
                await tx.api_tokens.find(account_id=lukas.account_id),
            )
            other = await tx.accounts.get(id=bob.account_id)
        tuples = await h.authz.read(account(lukas.account_id))
        user_tuples = await h.authz.read("account", subject=user(user_id))
        still = await h.client.get(bob.url(), headers=bob.headers)
        gone = await h.client.post(
            f"{API}/session", json={"username": "lukas", "password": PASSWORD}
        )
    assert (first.begun, first.pending, first.erased) == (0, 1, 0)  # begun before, see _begin
    assert after_fleet == ["fleet"]  # the others wait until the mowers' last states landed
    assert mid.confirmed == ["fleet"] and mid.state is ErasureState.PURGING
    assert second.erased == 1
    assert [s for s, _ in fake.calls] == ["fleet", "scheduler", "notifications"]
    assert done.state is ErasureState.ERASED and done.completed_at is not None
    assert done.counts == {"fleet": 7, "scheduler": 7, "notifications": 7}
    assert left == (None, None, [], [], [], [], [])
    assert tuples == [] and user_tuples == []
    assert other is not None and still.status_code == 200  # nobody else is touched
    assert gone.status_code == 401


async def test_a_failing_service_is_retried_with_backoff_and_skipped_services_stay_done() -> None:
    fake = FakeEraser()
    fake.fail = {"scheduler": 2}
    async with harness(eraser=fake, **NOW_GRACE) as h:
        lukas = await h.register("lukas")
        await _schedule(h, lukas)
        await _begin(h)
        await _erasure(h).sweep()
        h.clock.advance(timedelta(seconds=31))
        failed = await _erasure(h).sweep()
        first = await _record(h, lukas.account_id)
        not_due = await _erasure(h).sweep()  # inside the backoff
        h.clock.advance(timedelta(seconds=31))
        await _erasure(h).sweep()
        second = await _record(h, lukas.account_id)
        h.clock.advance(timedelta(minutes=2))
        done = await _erasure(h).sweep()
        async with h.store.transaction() as tx:
            acct = await tx.accounts.get(id=lukas.account_id)
    assert failed.pending == 1 and failed.erased == 0
    assert first.attempts == 1 and "scheduler" in (first.last_error or "")
    assert first.confirmed == ["fleet"]
    assert not_due.pending == 0  # nothing was due
    assert second.attempts == 2
    assert (second.next_attempt_at - first.next_attempt_at) > timedelta(0)  # type: ignore[operator]
    assert done.erased == 1 and acct is None
    # Fleet was asked once only, the failed service three times in all.
    assert [s for s, _ in fake.calls].count("fleet") == 1
    assert [s for s, _ in fake.calls] == ["fleet", "scheduler", "notifications"]


async def test_identity_keeps_its_rows_while_one_service_has_not_confirmed() -> None:
    fake = FakeEraser()
    fake.fail = {"notifications": 1000}
    async with harness(eraser=fake, **NOW_GRACE) as h:
        lukas = await h.register("lukas")
        await _schedule(h, lukas)
        await _begin(h)
        for _ in range(4):
            await _erasure(h).sweep()
            h.clock.advance(timedelta(minutes=11))
        record = await _record(h, lukas.account_id)
        async with h.store.transaction() as tx:
            acct = await tx.accounts.get(id=lukas.account_id)
            found = await tx.users.get(id=lukas.user_id)
    assert record.state is ErasureState.PURGING
    assert record.confirmed == ["fleet", "scheduler"]
    assert acct is not None and found is not None  # never half deleted
    assert record.attempts >= 3


async def test_the_purge_cuts_every_access_and_cannot_be_restored() -> None:
    fake = FakeEraser()
    async with harness(eraser=fake, **NOW_GRACE) as h:
        lukas = await h.register("lukas")
        await _schedule(h, lukas)
        await _begin(h)
        await _erasure(h).sweep()
        refresh = await h.client.post(
            f"{API}/session/refresh", json={"refresh_token": lukas.refresh_token}
        )
        login = await h.client.post(
            f"{API}/session", json={"username": "lukas", "password": PASSWORD}
        )
        restore = await h.client.post(lukas.url("/restore"), headers=lukas.headers)
        tuples = h.tuples_of(f"user:{lukas.user_id}")
    assert refresh.status_code == 401
    assert login.status_code == 401
    # Irreversible: the access token is on the deny list since the purge began (401), and
    # without it there is no tuple that would make the account visible.
    assert restore.status_code == 401
    assert tuples == set()


async def test_a_restore_before_the_purge_wins_and_nothing_is_asked() -> None:
    fake = FakeEraser()
    async with harness(eraser=fake, **{**NOW_GRACE, "identity_deletion_grace_days": 7}) as h:
        lukas = await h.register("lukas")
        await _schedule(h, lukas)
        login = await h.login("lukas")
        restored = await h.client.post(lukas.url("/restore"), headers=bearer(login["access_token"]))
        h.clock.advance(timedelta(days=8))
        sweep = await _erasure(h).sweep()
    assert restored.status_code == 200
    assert sweep.begun == 0 and fake.calls == []


async def test_the_audit_rows_stay_without_their_personal_fields() -> None:
    fake = FakeEraser()
    async with harness(eraser=fake, **NOW_GRACE) as h:
        lukas = await h.register("lukas")
        await _schedule(h, lukas)
        async with h.store.transaction() as tx:
            before = await tx.audit.find(account_id=lukas.account_id)
        await _finish(h)
        async with h.store.transaction() as tx:
            after = await tx.audit.find(account_id=lukas.account_id)
            everything = await tx.audit.find()
    assert any(e.details.get("username") == "lukas" for e in before)
    assert any(e.actor_name or e.ip for e in before)
    assert len(after) == len(before) + 1  # the rows stay, the start of the purge is one more
    assert {e.action for e in after} == {e.action for e in before} | {"account.erasure_started"}
    assert all("username" not in e.details and not e.actor_name and not e.ip for e in after)
    assert any(e.details.get("delete_after") for e in after)  # ids, counts and times stay
    final = [e for e in everything if e.action == "account.erased"]
    assert len(final) == 1
    assert final[0].target_id == str(lukas.account_id)
    assert final[0].details["services"] == ["fleet", "scheduler", "notifications"]
    assert "username" not in final[0].details and final[0].actor_name is None


async def test_scrubbing_twice_changes_nothing() -> None:
    fake = FakeEraser()
    async with harness(eraser=fake, **NOW_GRACE) as h:
        lukas = await h.register("lukas")
        async with h.store.transaction() as tx:
            first = await tx.scrub_audit(lukas.account_id)
            second = await tx.scrub_audit(lukas.account_id)
            other = await tx.scrub_audit(UUID(int=1))
    assert first > 0 and second == 0 and other == 0


async def test_two_replicas_do_not_run_the_same_purge() -> None:
    fake = FakeEraser()
    async with harness(eraser=fake, **NOW_GRACE) as h:
        lukas = await h.register("lukas")
        await _schedule(h, lukas)
        service = _erasure(h)
        await _begin(h)
        claimed = await service._claim_due()
        again = await service._claim_due()
    assert [e.account_id for e in claimed] == [lukas.account_id]
    assert again == []


async def test_begin_ignores_unknown_accounts_and_runs_only_once_per_account() -> None:
    fake = FakeEraser()
    async with harness(eraser=fake, **NOW_GRACE) as h:
        lukas = await h.register("lukas")
        await _schedule(h, lukas)
        service = _erasure(h)
        unknown = await service.begin(UUID(int=99))
        first = await service.begin(lukas.account_id)
        again = await service.begin(lukas.account_id)
    assert (unknown, first, again) == (False, True, False)


async def test_a_failure_in_identity_itself_is_retried_too(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeEraser()
    async with harness(eraser=fake, **{**NOW_GRACE, "identity_erasure_services": "fleet"}) as h:
        lukas = await h.register("lukas")
        await _schedule(h, lukas)
        service = _erasure(h)
        await _begin(h)

        async def broken(_: AccountErasure, __: int) -> None:
            msg = "database busy"
            raise RuntimeError(msg)

        real = service._erase_identity
        monkeypatch.setattr(service, "_erase_identity", broken)
        failed = await service.sweep()
        record = await _record(h, lukas.account_id)
        monkeypatch.setattr(service, "_erase_identity", real)
        h.clock.advance(timedelta(minutes=1))
        done = await service.sweep()
    assert failed.pending == 1 and "identity: RuntimeError" in (record.last_error or "")
    assert record.confirmed == ["fleet"]  # the service is not asked again
    assert done.erased == 1 and fake.calls == [("fleet", lukas.account_id)]


async def test_a_purge_that_stays_stuck_keeps_failing_loudly() -> None:
    fake = FakeEraser()
    fake.fail = {"fleet": 1000}
    async with harness(eraser=fake, **NOW_GRACE) as h:
        lukas = await h.register("lukas")
        await _schedule(h, lukas)
        await _begin(h)
        await _erasure(h).sweep()
        h.clock.advance(timedelta(hours=2))  # past stall_after: logged as an error
        stuck = await _erasure(h).sweep()
        record = await _record(h, lukas.account_id)
    assert stuck.pending == 1 and record.attempts == 2 and record.state is ErasureState.PURGING


async def test_the_loop_sweeps_and_survives_a_failing_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeEraser()
    async with harness(eraser=fake, **NOW_GRACE) as h:
        lukas = await h.register("lukas")
        await _schedule(h, lukas)
        await _begin(h)
        service = _erasure(h)
        task = asyncio.create_task(service.run(0.01))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        first_round = [s for s, _ in fake.calls]

        async def boom() -> None:
            msg = "no database"
            raise RuntimeError(msg)

        monkeypatch.setattr(service, "sweep", boom)
        task = asyncio.create_task(service.run(0.01))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        wait = await service.next_sweep_in(60)
    assert first_round[:1] == ["fleet"]
    assert 1.0 <= wait <= 60


async def test_nothing_is_deleted_without_a_configured_service() -> None:
    fake = FakeEraser()
    async with harness(eraser=fake, **{**NOW_GRACE, "identity_erasure_services": " , "}) as h:
        lukas = await h.register("lukas")
        await _schedule(h, lukas)
        sweep = await _erasure(h).sweep()
        async with h.store.transaction() as tx:
            acct = await tx.accounts.get(id=lukas.account_id)
    assert sweep.begun == 0 and fake.calls == []
    assert acct is not None and acct.status is AccountStatus.DELETING


async def test_the_demo_runs_no_account_erasure() -> None:
    async with harness(demo_mode=True, **DEMO) as h:
        assert h.app.state.identity.erasure is None


async def test_the_purge_is_off_by_default_and_nothing_changes() -> None:
    """While the switch is off no purge starts: an account stays deleting and restorable past
    delete_after, exactly as before the purge existed."""
    fake = FakeEraser()
    off = {k: v for k, v in NOW_GRACE.items() if k != "identity_erasure_enabled"}
    assert make_settings().identity_erasure_enabled is False
    async with harness(eraser=fake, **off) as h:
        assert h.app.state.identity.erasure is None  # not wired, no background task
        lukas = await h.register("lukas")
        await _schedule(h, lukas)
        # delete_after is "now" (the grace is 0 days here): the grace period is over.
        restored = await h.client.post(lukas.url("/restore"), headers=lukas.headers)
        async with h.store.transaction() as tx:
            records = await tx.erasures.find()
    assert fake.calls == [] and records == []
    assert restored.status_code == 200 and restored.json()["status"] == "active"


async def test_switching_it_off_says_so_at_startup(capsys: pytest.CaptureFixture[str]) -> None:
    async with harness(identity_erasure_enabled=False):
        pass
    off = capsys.readouterr()
    async with harness(identity_erasure_enabled=True):
        pass
    on = capsys.readouterr()
    assert "account erasure is switched off" in off.out + off.err
    assert "account erasure is switched off" not in on.out + on.err


async def test_the_switch_never_turns_the_purge_on_in_the_demo() -> None:
    assert make_settings(identity_erasure_enabled=True).erasure_enabled is True
    assert make_settings(identity_erasure_enabled=True, demo_mode=True).erasure_enabled is False


async def test_the_default_services_are_the_core_ones_in_order() -> None:
    config = make_settings().erasure_config()
    assert config.services[0] == "fleet"
    assert "billing" not in config.services  # an installation with billing adds it
    assert set(config.services) == {
        "fleet",
        "scheduler",
        "telemetry",
        "notifications",
        "vision",
        "planner",
        "entitlements",
    }


async def test_a_stalled_purge_is_reported() -> None:
    erasure = AccountErasure.begin(UUID(int=2), ["fleet"], datetime.now(UTC))
    later = erasure.started_at + timedelta(hours=2)
    assert erasure.stalled(later, timedelta(hours=1))
    assert not erasure.stalled(erasure.started_at + timedelta(minutes=5), timedelta(hours=1))
    erasure.finish(later)
    assert not erasure.stalled(later, timedelta(hours=1))


async def test_the_operator_sees_open_and_finished_purges() -> None:
    fake = FakeEraser()
    fake.fail = {"scheduler": 1000}
    async with harness(eraser=fake, **NOW_GRACE) as h:
        lukas = await h.register("lukas")
        await _schedule(h, lukas)
        await _begin(h)
        await _erasure(h).sweep()
        h.clock.advance(timedelta(seconds=31))
        await _erasure(h).sweep()
        runtime = h.app.state.runtime

        async def run(*argv: str) -> tuple[int, str]:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = await amain(list(argv), cast(Runtime, _NoClose(runtime)))
            return code, buffer.getvalue()

        code, out = await run("erasures")
        code_all, out_all = await run("erasures", "--all")
    assert code == 0 and str(lukas.account_id) in out
    assert "confirmed 1/3" in out and "waiting for scheduler, notifications" in out
    assert "scheduler is down" in out
    assert code_all == 0 and "purging" in out_all


class _NoClose:
    """The harness runtime for the CLI without closing the app's connections."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime
        self.settings = runtime.settings

    async def start(self) -> Any:
        return self._runtime.services

    async def close(self) -> None:
        return None


async def test_the_cli_says_so_when_nothing_is_open() -> None:
    fake = FakeEraser()
    async with harness(eraser=fake, **NOW_GRACE) as h:
        runtime = h.app.state.runtime
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            await amain(["erasures"], cast(Runtime, _NoClose(runtime)))
        both = io.StringIO()
        with redirect_stdout(both):
            await amain(["erasures", "--all"], cast(Runtime, _NoClose(runtime)))
    assert buffer.getvalue().strip() == "no purge is open"
    assert both.getvalue().strip() == "no account was erased yet"


# --- the NATS adapter ----------------------------------------------------------------------


async def _serve(bus: InMemoryBus, service: str, reply: AccountEraseReply) -> list[UUID]:
    seen: list[UUID] = []

    async def handle(message: Any) -> None:
        seen.append(message.model(AccountEraseRequest).account_id)
        await message.respond(reply)

    await bus.subscribe(f"account.erase.{service}", handle)
    return seen


async def test_the_bus_eraser_sends_the_request_and_returns_the_count() -> None:
    bus = InMemoryBus()
    seen = await _serve(bus, "fleet", AccountEraseReply(ok=True, deleted=12))
    target = UUID(int=5)
    assert await BusAccountEraser(bus).erase("fleet", target) == 12
    assert seen == [target]


async def test_the_bus_eraser_raises_on_a_refusal_no_answer_and_no_bus() -> None:
    bus = InMemoryBus()
    await _serve(bus, "fleet", AccountEraseReply(ok=False, error="database down"))
    with pytest.raises(RuntimeError, match="fleet refused: database down"):
        await BusAccountEraser(bus).erase("fleet", UUID(int=5))
    with pytest.raises(Exception):  # noqa: B017 - the bus names its own error type
        await BusAccountEraser(bus, timeout=0.05).erase("scheduler", UUID(int=5))
    with pytest.raises(RuntimeError, match="no event bus"):
        await BusAccountEraser(None).erase("fleet", UUID(int=5))


# --- nobody joins and no token is made while the account is on its way out ---------------


async def test_a_deleting_account_takes_no_new_members_and_no_new_tokens() -> None:
    """ADR 0082: a member added now would be erased without being told, and a token could never
    be exchanged. Everything else the owner does stays allowed; restore re-opens it."""
    async with harness() as h:
        lukas = await h.register("lukas")
        level = {"type": "account", "id": str(lukas.account_id)}
        await _schedule(h, lukas)
        sub = {"username": "anna", "password": PASSWORD, "assignments": []}
        attempts = {
            "subuser": await h.client.post(lukas.url("/users"), headers=lukas.headers, json=sub),
            "token": await h.client.post(
                lukas.url("/api-tokens"),
                headers=lukas.headers,
                json={"name": "t", "grants": [{"level": level, "permissions": ["mower.view"]}]},
            ),
            "personal": await h.client.post(
                f"{API}/me/api-tokens",
                headers=lukas.headers,
                json={"name": "p", "scopes": ["mower.view"], "expires_in_days": 30},
            ),
            "widget": await h.client.post(
                f"{API}/me/widget-credentials",
                headers=lukas.headers,
                json={"device_name": "iPhone"},
            ),
        }
        stop_allowed = await h.client.patch(lukas.url(), headers=lukas.headers, json={"name": "x"})
        members = await h.client.get(lukas.url("/users"), headers=lukas.headers)
        restored = await h.client.post(lukas.url("/restore"), headers=lukas.headers)
        again = await h.client.post(lukas.url("/users"), headers=lukas.headers, json=sub)
        async with h.store.transaction() as tx:
            tokens = await tx.api_tokens.find(account_id=lukas.account_id, revoked_at=None)
    for name, response in attempts.items():
        assert response.status_code == 409, (name, response.text)
        assert "scheduled for deletion" in response.json()["detail"]
        assert "cancel the deletion first" in response.json()["detail"]
    assert stop_allowed.status_code == 200  # renaming and the rest stay allowed
    assert len(members.json()) == 1  # nobody was added
    assert restored.status_code == 200 and again.status_code == 201
    assert tokens == []


async def test_a_purging_account_is_refused_the_same_way() -> None:
    from identity.application.accounts import ensure_not_scheduled_for_deletion
    from identity.domain.errors import ConflictError

    fake = FakeEraser()
    async with harness(eraser=fake, **NOW_GRACE) as h:
        lukas = await h.register("lukas")
        await _schedule(h, lukas)
        await _erasure(h).begin_due()
        async with h.store.transaction() as tx:
            with pytest.raises(ConflictError, match="cancel the deletion first"):
                await ensure_not_scheduled_for_deletion(tx, lukas.account_id)
            # An unknown account is not this guard's business (the callers 404 before).
            await ensure_not_scheduled_for_deletion(tx, UUID(int=7))


async def test_an_active_account_passes_the_guard() -> None:
    from identity.application.accounts import ensure_not_scheduled_for_deletion

    async with harness() as h:
        lukas = await h.register("lukas")
        async with h.store.transaction() as tx:
            await ensure_not_scheduled_for_deletion(tx, lukas.account_id)
