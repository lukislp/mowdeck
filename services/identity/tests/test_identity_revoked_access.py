# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""No access token outlives a deletion (ADR 0082, section 12).

Access tokens are verified offline and live for minutes. Two rules close the gap between the
end of the access and the end of its tokens:

* the purge of an account asks no service before the last token issued for the account has
  expired (a pure rule with the lifetime the issuer signs with), and a purging account gets no
  new token;
* ``identity.access.revoked`` tells every service to refuse the tokens that still exist: for
  the account when its purge starts, for a deleted user and the API tokens that ended with
  them.

Nothing changes for a sign-out, a refresh or the owner during the grace period.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from identity_support import API, PASSWORD, Harness, bearer, harness
from mowdeck_contracts.v1 import (
    IDENTITY_ACCESS_REVOKED,
    IDENTITY_USER_DELETED,
    AccessRevoked,
    AccessScope,
)
from mowdeck_contracts.v1.subjects import ALL_ACCESS_REVOKED, access_revoked_subject
from test_identity_erasure import NOW_GRACE, FakeEraser, _erasure, _record, _schedule

from identity.domain.erasure import TOKEN_SKEW, AccountErasure, tokens_expired_at
from identity.domain.models import AccountStatus

ME = f"{API}/me"
T0 = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
TEN = timedelta(minutes=10)


def revocations(h: Harness) -> list[tuple[str, AccessRevoked, dict[str, Any]]]:
    """The deny list entries Identity published: subject, payload and the whole envelope."""
    found = []
    for message in h.bus.messages(ALL_ACCESS_REVOKED):
        envelope = json.loads(message.data)
        assert envelope["type"] == IDENTITY_ACCESS_REVOKED
        found.append((message.subject, AccessRevoked.model_validate(envelope["payload"]), envelope))
    return found


# --- the rule --------------------------------------------------------------------------------


def test_tokens_have_expired_one_lifetime_and_the_skew_after_the_access_ended() -> None:
    assert timedelta(seconds=60) == TOKEN_SKEW
    assert tokens_expired_at(T0, TEN) == T0 + timedelta(minutes=11)
    assert tokens_expired_at(T0, timedelta(minutes=1)) == T0 + timedelta(minutes=2)
    assert tokens_expired_at(T0, TEN, timedelta(0)) == T0 + TEN


def test_a_purge_that_begins_is_due_when_the_tokens_have_expired() -> None:
    account_id = UUID(int=1)
    at_once = AccountErasure.begin(account_id, ["fleet"], T0)
    waiting = AccountErasure.begin(account_id, ["fleet"], T0, ask_from=tokens_expired_at(T0, TEN))
    assert at_once.due(T0)
    assert not waiting.due(T0 + timedelta(minutes=10, seconds=59))
    assert waiting.due(T0 + timedelta(minutes=11))
    assert waiting.started_at == T0


# --- the purge job waits ---------------------------------------------------------------------


async def test_no_service_is_asked_while_a_token_of_the_account_can_still_be_valid() -> None:
    fake = FakeEraser()
    async with harness(eraser=fake, **NOW_GRACE) as h:
        lukas = await h.register("lukas")
        await _schedule(h, lukas)

        begun = await _erasure(h).sweep()
        record = await _record(h, lukas.account_id)
        async with h.store.transaction() as tx:
            account = await tx.accounts.get(id=lukas.account_id)
        asked_at_once = list(fake.calls)
        h.clock.advance(timedelta(minutes=10, seconds=59))
        early = await _erasure(h).sweep()
        asked_early = list(fake.calls)
        h.clock.advance(timedelta(seconds=1))
        due = await _erasure(h).sweep()

    assert begun.begun == 1 and account is not None and account.status is AccountStatus.PURGING
    assert asked_at_once == []
    assert record.next_attempt_at == record.started_at + TEN + TOKEN_SKEW
    assert (early.pending, asked_early) == (0, [])
    assert due.pending == 1 and fake.calls == [("fleet", lukas.account_id)]


async def test_the_wait_is_the_lifetime_the_issuer_signs_with() -> None:
    """One setting: a shorter token lifetime shortens the wait, nothing is written down twice."""
    fake = FakeEraser()
    async with harness(eraser=fake, identity_access_token_minutes=3, **NOW_GRACE) as h:
        lukas = await h.register("lukas")
        issued = await h.login("lukas")
        await _schedule(h, lukas)
        await _erasure(h).sweep()
        record = await _record(h, lukas.account_id)
        h.clock.advance(timedelta(minutes=3, seconds=59))
        await _erasure(h).sweep()
        early = list(fake.calls)
        h.clock.advance(timedelta(seconds=1))
        await _erasure(h).sweep()
    assert issued["expires_in"] == 180
    assert record.next_attempt_at == record.started_at + timedelta(minutes=4)
    assert early == [] and fake.calls == [("fleet", lukas.account_id)]


async def test_a_purge_recorded_as_due_at_once_is_held_back_too() -> None:
    """A record written before this rule (or by hand) cannot get around it."""
    fake = FakeEraser()
    async with harness(eraser=fake, **NOW_GRACE) as h:
        lukas = await h.register("lukas")
        await _schedule(h, lukas)
        await _erasure(h).begin_due()
        async with h.store.transaction() as tx:
            record = await tx.erasures.get(account_id=lukas.account_id)
            assert record is not None
            record.next_attempt_at = record.started_at
            await tx.erasures.update(record)

        sweep = await _erasure(h).sweep()
        held = await _record(h, lukas.account_id)

    assert fake.calls == [] and sweep.pending == 1
    assert held.next_attempt_at == held.started_at + TEN + TOKEN_SKEW
    assert held.attempts == 0 and held.last_error is None  # waiting is not a failure


# --- a purging account -----------------------------------------------------------------------


async def test_the_start_of_the_purge_ends_every_session_and_issues_no_token_again() -> None:
    fake = FakeEraser()
    async with harness(eraser=fake, **NOW_GRACE) as h:
        lukas = await h.register("lukas")
        session = await h.login("lukas")
        await _schedule(h, lukas)

        await _erasure(h).sweep()

        refresh = await h.client.post(
            f"{API}/session/refresh", json={"refresh_token": session["refresh_token"]}
        )
        login = await h.client.post(
            f"{API}/session", json={"username": "lukas", "password": PASSWORD}
        )
        mine = await h.client.get(ME, headers=bearer(session["access_token"]))
        record = await _record(h, lukas.account_id)
        entries = revocations(h)
    assert refresh.status_code == 401 and login.status_code in (401, 403)
    assert mine.status_code == 401  # Identity refuses the access token of the ended session
    [(subject, entry, envelope)] = entries
    assert subject == access_revoked_subject(lukas.account_id)
    assert (entry.scope, entry.id) == (AccessScope.ACCOUNT, lukas.account_id)
    assert entry.issued_before == record.started_at
    assert entry.expires_at == record.next_attempt_at == record.started_at + TEN + TOKEN_SKEW
    assert envelope["account_id"] == str(lukas.account_id)
    assert set(envelope["payload"]) == {"scope", "id", "issued_before", "expires_at"}  # ids only


async def test_the_owner_keeps_full_access_during_the_grace_period() -> None:
    """The maintainer's decision stands: nothing is revoked while the account is ``deleting``."""
    fake = FakeEraser()
    async with harness(eraser=fake, **{**NOW_GRACE, "identity_deletion_grace_days": 7}) as h:
        lukas = await h.register("lukas")
        session = await h.login("lukas")
        await _schedule(h, lukas)
        await _erasure(h).sweep()  # inside the grace period: nothing begins

        refresh = await h.client.post(
            f"{API}/session/refresh", json={"refresh_token": session["refresh_token"]}
        )
        again = await h.login("lukas")
        mine = await h.client.get(ME, headers=bearer(again["access_token"]))
        restored = await h.client.post(lukas.url("/restore"), headers=bearer(again["access_token"]))
        entries = revocations(h)
    assert refresh.status_code == 200 and mine.status_code == 200
    assert restored.status_code == 200
    assert entries == [] and fake.calls == []


# --- a deleted user --------------------------------------------------------------------------


async def test_delete_me_revokes_the_user_and_their_tokens_before_the_services_hear_of_it() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        level = {"type": "account", "id": str(lukas.account_id)}
        admin = await h.role_id(lukas, "admin")
        user_id, token = await h.subuser(lukas, "anna", [{"role_id": str(admin), "level": level}])
        personal = await h.client.post(
            f"{ME}/api-tokens",
            headers=bearer(token),
            json={"name": "script", "scopes": ["mower.view"], "expires_in_days": 30},
        )
        assert personal.status_code == 201, personal.text
        before = h.clock()

        response = await h.client.request(
            "DELETE", ME, headers=bearer(token), json={"password": PASSWORD}
        )

        assert response.status_code == 200, response.text
        entries = revocations(h)
        subjects = [m.subject for m in h.bus.published]
    revoked = {entry.id for _, entry, _ in entries}
    assert user_id in revoked and len(revoked) == 2  # the user and the personal access token
    assert UUID(personal.json()["id"]) in revoked
    for subject, entry, _ in entries:
        assert subject == access_revoked_subject(entry.id)
        assert entry.scope is AccessScope.SUBJECT
        assert entry.issued_before >= before
        assert entry.expires_at == entry.issued_before + TEN + TOKEN_SKEW
    # The services refuse the tokens before they drop the user's rows.
    deleted_at = subjects.index(f"event.{IDENTITY_USER_DELETED}")
    assert all(subjects.index(subject) < deleted_at for subject, _, _ in entries)
    assert lukas.user_id not in revoked  # nobody else


async def test_an_admin_who_deletes_a_sub_user_revokes_that_users_tokens_too() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        user_id, _ = await h.subuser(lukas, "anna")
        response = await h.client.request(
            "DELETE",
            lukas.url(f"/users/{user_id}"),
            headers=lukas.headers,
            json={"password": PASSWORD},
        )
        assert response.status_code in (200, 204), response.text
        entries = revocations(h)
    assert [(entry.scope, entry.id) for _, entry, _ in entries] == [(AccessScope.SUBJECT, user_id)]


# --- everything else stays as it was ---------------------------------------------------------


async def test_sign_out_refresh_and_disabling_revoke_nothing() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        user_id, _ = await h.subuser(lukas, "anna")
        session = await h.login("lukas")
        refreshed = await h.client.post(
            f"{API}/session/refresh", json={"refresh_token": session["refresh_token"]}
        )
        assert refreshed.status_code == 200
        out = await h.client.post(
            f"{API}/session/logout", json={"refresh_token": refreshed.json()["refresh_token"]}
        )
        everywhere = await h.client.post(f"{API}/session/logout-all", headers=lukas.headers)
        assert out.status_code < 300 and everywhere.status_code < 300
        assert revocations(h) == []
        assert user_id is not None
