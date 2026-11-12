# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

""" "Delete me" (ADR 0082, step 3): ``DELETE /identity/me`` applies the operation of the
caller's role. A member who is not the owner is deleted at once (the user, every credential and
session, the tuples, the personal tokens; the audit rows stay without personal fields); the
owner's request is the account deletion. Who may ask, what stays with the account, the demo."""

import dataclasses
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from identity_support import (
    API,
    PASSWORD,
    Account,
    FlakyBackend,
    Harness,
    bearer,
    harness,
)
from mowdeck_authz.testing import InMemoryAuthorizer
from mowdeck_contracts.v1 import EventEnvelope, Principal, PrincipalKind, UserChanged
from mowdeck_service_kit.demo import DEMO_SEED_HEADER

import identity
from identity.application.context import SYSTEM, RequestContext
from identity.application.users import OWNER_DELETES_THE_ACCOUNT
from identity.domain.erasure import MEMBER_OF_ANOTHER_ACCOUNT, OWNS_ANOTHER_ACCOUNT
from identity.domain.errors import ConflictError, ForbiddenError, NotFoundError
from identity.domain.models import AccountStatus, KnownDevice, Membership, Passkey, Status

ME = f"{API}/me"
REAUTH = "urn:mowdeck:problem:reauth-required"
STEP_UP_UNAVAILABLE = "urn:mowdeck:problem:step-up-unavailable"
DELETED = "event.identity.user.deleted"
WRONG = "not the password"


async def _delete_me(h: Harness, token: str, password: str | None = PASSWORD) -> Any:
    body = {"password": password} if password is not None else None
    return await h.client.request("DELETE", ME, headers=bearer(token), json=body)


async def _admin(h: Harness, owner: Account, username: str) -> tuple[UUID, str]:
    """A sub-user with the Admin role on the account."""
    level = {"type": "account", "id": str(owner.account_id)}
    admin = await h.role_id(owner, "admin")
    return await h.subuser(owner, username, [{"role_id": str(admin), "level": level}])


def _rows_of_user(h: Harness, user_id: UUID) -> dict[str, int]:
    """Rows of every table of the store that name the user (``user_id``, or the user's row)."""
    found: dict[str, int] = {}
    for table in dataclasses.fields(h.store.tables):
        rows = getattr(h.store.tables, table.name).rows.values()
        count = sum(
            1
            for row in rows
            if getattr(row, "user_id", None) == user_id
            or (table.name == "users" and row.id == user_id)
            or (table.name == "assignments" and row.subject_id == user_id)
        )
        if count:
            found[table.name] = count
    return found


# --- a member deletes themselves ---------------------------------------------------------------


async def test_a_member_is_deleted_at_once_and_the_account_goes_on() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        operator = await h.role_id(lukas, "operator")
        level = {"type": "account", "id": str(lukas.account_id)}
        anna_id, anna = await h.subuser(lukas, "anna", [{"role_id": str(operator), "level": level}])
        ben_id, ben = await h.subuser(lukas, "ben")
        await h.client.put(
            f"{ME}/onboarding",
            headers=bearer(anna),
            json={"status": "completed", "skipped_steps": [], "dismissed_hints": []},
        )
        async with h.store.transaction() as tx:
            await tx.passkeys.add(_passkey(anna_id, h))
            await tx.known_devices.add(_device(anna_id, h))
        before = _rows_of_user(h, anna_id)
        tuples_before = h.tuples_of(f"user:{anna_id}")

        response = await _delete_me(h, anna)

        left = _rows_of_user(h, anna_id)
        members = (await h.client.get(lukas.url("/users"), headers=lukas.headers)).json()
        ben_me = await h.client.get(ME, headers=bearer(ben))
        account = (await h.client.get(lukas.url(), headers=lukas.headers)).json()
        events = [m.model(EventEnvelope[UserChanged]) for m in h.bus.messages(DELETED)]
    assert response.status_code == 200, response.text
    assert response.json() == {
        "operation": "user",
        "account_id": str(lukas.account_id),
        "delete_after": None,
    }
    assert response.headers["cache-control"] == "no-store"
    assert {"users", "memberships", "sessions", "assignments", "passkeys", "known_devices"} <= set(
        before
    )
    assert "onboarding" in before and tuples_before
    assert left == {}
    assert h.tuples_of(f"user:{anna_id}") == set()
    assert sorted(m["username"] for m in members) == ["ben", "lukas"]
    assert ben_me.status_code == 200 and h.tuples_of(f"user:{ben_id}")
    assert account["status"] == AccountStatus.ACTIVE.value and account["delete_after"] is None
    assert [(e.payload.user_id, e.payload.account_id, e.payload.subuser) for e in events] == [
        (anna_id, lukas.account_id, True)
    ]
    assert events[0].payload.reason == "self"


async def test_a_deleted_user_is_signed_out_everywhere_and_cannot_sign_in() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        await h.subuser(lukas, "anna")
        second = await h.login("anna")  # another device
        first = await h.login("anna")
        response = await _delete_me(h, first["access_token"])
        me_first = await h.client.get(ME, headers=bearer(first["access_token"]))
        me_second = await h.client.get(ME, headers=bearer(second["access_token"]))
        refresh = await h.client.post(
            f"{API}/session/refresh", json={"refresh_token": second["refresh_token"]}
        )
        login = await h.client.post(
            f"{API}/session", json={"username": "anna", "password": PASSWORD}
        )
        again = await _delete_me(h, first["access_token"])
    assert response.status_code == 200
    assert "mowdeck_refresh" in response.headers.get("set-cookie", "")
    assert (me_first.status_code, me_second.status_code) == (401, 401)
    assert refresh.status_code == 401
    assert login.status_code == 401
    assert again.status_code == 401  # a retry finds no session: nothing happens twice


async def test_the_username_and_the_address_are_free_again() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        body = {"username": "anna", "password": PASSWORD, "email": "anna@example.org"}
        created = await h.client.post(lukas.url("/users"), headers=lukas.headers, json=body)
        anna = (await h.login("anna"))["access_token"]
        taken = await h.client.post(lukas.url("/users"), headers=lukas.headers, json=body)
        deleted = await _delete_me(h, anna)
        again = await h.client.post(lukas.url("/users"), headers=lukas.headers, json=body)
    assert (created.status_code, taken.status_code) == (201, 409)
    assert deleted.status_code == 200
    assert again.status_code == 201 and again.json()["id"] != created.json()["id"]


async def test_other_accounts_and_other_members_keep_everything() -> None:
    async with harness() as h:
        lukas, bob = await h.register("lukas"), await h.register("bob")
        _, anna = await h.subuser(lukas, "anna")
        carl_id, carl = await h.subuser(bob, "carl")
        ben_id, _ = await h.subuser(lukas, "ben")
        untouched = {
            user_id: (_rows_of_user(h, user_id), h.tuples_of(f"user:{user_id}"))
            for user_id in (lukas.user_id, bob.user_id, carl_id, ben_id)
        }
        bob_before = await h.audit(bob)
        await _delete_me(h, anna)
        after = {
            user_id: (_rows_of_user(h, user_id), h.tuples_of(f"user:{user_id}"))
            for user_id in untouched
        }
        carl_me = await h.client.get(ME, headers=bearer(carl))
        bob_after = await h.audit(bob)
    assert after == untouched
    assert carl_me.status_code == 200
    assert bob_after == bob_before and any(e["ip"] for e in bob_after)


# --- who may ask -------------------------------------------------------------------------------


async def test_it_needs_the_password_or_a_recent_confirmation() -> None:
    async with harness() as h:
        # Sign in 6 minutes "ago" (a token issued in the future would not verify), then come back.
        h.clock.advance(-timedelta(minutes=6))
        lukas = await h.register("lukas")
        anna_id, anna = await h.subuser(lukas, "anna")
        h.clock.advance(timedelta(minutes=6))
        stale = await _delete_me(h, anna, password=None)
        wrong = await _delete_me(h, anna, password=WRONG)
        still_there = _rows_of_user(h, anna_id)
        failed = await h.audit(lukas, action="user.reauth_failed")
        step = await h.client.post(
            f"{ME}/step-up/password", headers=bearer(anna), json={"password": PASSWORD}
        )
        confirmed = await _delete_me(h, step.json()["access_token"], password=None)
    assert stale.status_code == 401 and stale.json()["type"] == REAUTH
    assert wrong.status_code == 401 and wrong.json()["type"] != REAUTH
    assert "users" in still_there
    assert [entry["outcome"] for entry in failed] == ["failure"]
    assert confirmed.status_code == 200


async def test_wrong_passwords_are_throttled_like_logins() -> None:
    async with harness(identity_login_max_failures=3) as h:
        lukas = await h.register("lukas")
        _, anna = await h.subuser(lukas, "anna")
        answers = [await _delete_me(h, anna, password=WRONG) for _ in range(4)]
        right = await _delete_me(h, anna)
    assert [a.status_code for a in answers] == [401, 401, 401, 429]
    assert right.status_code == 429  # the throttle holds for the right password too


async def test_api_tokens_cannot_delete_anybody() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        level = {"type": "account", "id": str(lukas.account_id)}
        operator = await h.role_id(lukas, "operator")
        anna_id, anna = await h.subuser(lukas, "anna", [{"role_id": str(operator), "level": level}])
        integration = await h.client.post(
            lukas.url("/api-tokens"),
            headers=lukas.headers,
            json={"name": "ha", "grants": [{"level": level, "permissions": ["mower.view"]}]},
        )
        personal = await h.client.post(
            f"{ME}/api-tokens",
            headers=bearer(anna),
            json={"name": "script", "scopes": ["mower.view"], "expires_in_days": 30},
        )
        answers = []
        for secret in (integration.json()["token"], personal.json()["token"]):
            access = (await h.client.post(f"{API}/token", json={"token": secret})).json()
            answers.append(await _delete_me(h, access["access_token"]))
            answers.append(await _delete_me(h, access["access_token"], password=None))
        anonymous = await h.client.request("DELETE", ME)
    assert [(a.status_code, a.json()["type"]) for a in answers] == [(403, STEP_UP_UNAVAILABLE)] * 4
    assert anonymous.status_code == 401
    assert "users" in _rows_of_user(h, anna_id) and "users" in _rows_of_user(h, lukas.user_id)


async def test_the_use_case_refuses_callers_without_a_login_or_an_account() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        service = h.app.state.identity.self_deletion
        stranger = Principal(subject=uuid4(), account_id=lukas.account_id, kind=PrincipalKind.USER)
        unbound = Principal(subject=lukas.user_id, kind=PrincipalKind.USER)
        with pytest.raises(ForbiddenError, match="login required"):
            await service.delete_me(PASSWORD, SYSTEM)
        with pytest.raises(ForbiddenError, match="not a member"):
            await service.delete_me(PASSWORD, RequestContext(principal=stranger))
        with pytest.raises(ForbiddenError, match="not bound to an account"):
            await service.delete_me(PASSWORD, RequestContext(principal=unbound))
        with pytest.raises(ForbiddenError, match="login required"):
            await h.app.state.identity.users.delete_self(lukas.account_id, SYSTEM)
        with pytest.raises(NotFoundError):
            await h.app.state.identity.users.delete_self(
                lukas.account_id, RequestContext(principal=stranger)
            )


# --- the owner ---------------------------------------------------------------------------------


async def test_the_owners_request_is_the_account_deletion() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        response = await _delete_me(h, lukas.token)
        account = (await h.client.get(lukas.url(), headers=lukas.headers)).json()
        me = await h.client.get(ME, headers=lukas.headers)
        again = await _delete_me(h, lukas.token)
        restored = await h.client.post(lukas.url("/restore"), headers=lukas.headers)
        events = h.bus.messages(DELETED)
    body = response.json()
    assert response.status_code == 202, response.text
    assert body["operation"] == "account" and body["account_id"] == str(lukas.account_id)
    assert body["delete_after"] is not None
    assert account["status"] == AccountStatus.DELETING.value
    assert account["delete_after"].replace("Z", "+00:00") == body["delete_after"].replace(
        "Z", "+00:00"
    )
    assert me.status_code == 200  # the owner keeps the session and every permission
    assert "mowdeck_refresh" not in response.headers.get("set-cookie", "")
    # While the account is ``deleting`` the owner is its only member: the same 409 as step 1.
    assert again.status_code == 409 and "already scheduled" in again.json()["detail"]
    assert restored.status_code == 200
    assert events == []


async def test_the_owner_of_an_account_with_members_is_told_to_remove_them_first() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        _, anna = await h.subuser(lukas, "anna")
        blocked = await _delete_me(h, lukas.token)
        gone = await _delete_me(h, anna)  # the last other member leaves by themselves
        scheduled = await _delete_me(h, lukas.token)
    assert blocked.status_code == 409 and "1 other member" in blocked.json()["detail"]
    assert gone.status_code == 200
    assert scheduled.status_code == 202 and scheduled.json()["operation"] == "account"


async def test_the_owner_needs_the_same_confirmation() -> None:
    async with harness() as h:
        h.clock.advance(-timedelta(minutes=6))
        lukas = await h.register("lukas")
        h.clock.advance(timedelta(minutes=6))
        stale = await _delete_me(h, lukas.token, password=None)
        wrong = await _delete_me(h, lukas.token, password=WRONG)
        account = (await h.client.get(lukas.url(), headers=lukas.headers)).json()
    assert stale.status_code == 401 and stale.json()["type"] == REAUTH
    assert wrong.status_code == 401
    assert account["status"] == AccountStatus.ACTIVE.value


async def test_the_role_decides_not_the_kind_of_user() -> None:
    """A person who is no longer the owner deletes only themselves; a sub-user who became the
    owner deletes the account."""
    async with harness() as h:
        lukas = await h.register("lukas")
        anna_id, _ = await h.subuser(lukas, "anna")
        await h.app.state.identity.accounts.promote_owner(lukas.account_id, "anna", SYSTEM)
        former = await _delete_me(h, lukas.token)
        owner = await _delete_me(h, (await h.login("anna"))["access_token"])
        with pytest.raises(ConflictError) as refused:
            await h.app.state.identity.users.delete_self(
                lukas.account_id,
                RequestContext(
                    principal=Principal(
                        subject=anna_id, account_id=lukas.account_id, kind=PrincipalKind.SUBUSER
                    )
                ),
            )
    assert former.status_code == 200 and former.json()["operation"] == "user"
    assert _rows_of_user(h, lukas.user_id) == {}
    assert owner.status_code == 202 and owner.json()["operation"] == "account"
    assert refused.value.message == OWNER_DELETES_THE_ACCOUNT
    assert "users" in _rows_of_user(h, anna_id)


# --- one person, one account -------------------------------------------------------------------


async def test_me_says_in_advance_which_deletion_applies() -> None:
    """The client shows the right dialog before the call: ``GET /me`` names the operation."""
    async with harness() as h:
        lukas = await h.register("lukas")
        _, anna = await h.subuser(lukas, "anna")
        owner = (await h.client.get(ME, headers=lukas.headers)).json()["memberships"]
        member = (await h.client.get(ME, headers=bearer(anna))).json()["memberships"]
        deleted = await _delete_me(h, anna)
        scheduled = await _delete_me(h, lukas.token)
        deleting = (await h.client.get(ME, headers=lukas.headers)).json()["memberships"]
    assert [(m["is_owner"], m["self_deletion"]) for m in owner] == [(True, "account")]
    assert [(m["is_owner"], m["self_deletion"]) for m in member] == [(False, "user")]
    # The answers of the call match what was announced.
    assert (deleted.json()["operation"], scheduled.json()["operation"]) == ("user", "account")
    assert deleting[0]["account_status"] == AccountStatus.DELETING.value


async def test_me_names_the_refusal_the_member_count_and_the_date() -> None:
    """What the app's danger area and its banner read from ``GET /me`` alone (step 4): why the
    deletion is refused, how many members block the owner, and when a scheduled account goes."""
    async with harness() as h:
        lukas = await h.register("lukas")
        _, anna = await h.subuser(lukas, "anna")
        with_anna = (await h.client.get(ME, headers=lukas.headers)).json()["memberships"][0]
        member = (await h.client.get(ME, headers=bearer(anna))).json()["memberships"][0]
        await _delete_me(h, anna)
        alone = (await h.client.get(ME, headers=lukas.headers)).json()["memberships"][0]
        scheduled = (await _delete_me(h, lukas.token)).json()
        deleting = (await h.client.get(ME, headers=lukas.headers)).json()["memberships"][0]
        await h.client.post(lukas.url("/restore"), headers=lukas.headers)
        restored = (await h.client.get(ME, headers=lukas.headers)).json()["memberships"][0]
    assert (with_anna["self_deletion_blocked"], with_anna["other_members"]) == ("other_members", 1)
    assert (member["self_deletion_blocked"], member["other_members"]) == (None, None)
    assert (with_anna["deletion_grace_days"], member["deletion_grace_days"]) == (7, None)
    assert member["delete_after"] is None
    assert (alone["self_deletion_blocked"], alone["other_members"]) == (None, 0)
    assert deleting["account_status"] == AccountStatus.DELETING.value
    assert deleting["delete_after"].replace("Z", "+00:00") == scheduled["delete_after"].replace(
        "Z", "+00:00"
    )
    assert (restored["account_status"], restored["delete_after"]) == ("active", None)


async def test_every_user_has_exactly_one_membership() -> None:
    """The invariant the deletion relies on: a membership is only ever created together with its
    user (a registration, a new sub-user), so nobody is in two accounts. Every way a user or a
    membership comes to be is walked here; the source is pinned too, so a new place that adds a
    membership (invitations) fails this test and has to look at ``self_deletion_blocker``."""
    async with harness() as h:
        lukas, bob = await h.register("lukas"), await h.register("bob")
        await h.subuser(lukas, "anna")
        await _admin(h, bob, "ada")
        await h.app.state.identity.accounts.promote_owner(lukas.account_id, "anna", SYSTEM)
        async with h.store.transaction() as tx:
            users = await tx.users.find()
            per_user = [len(await tx.memberships.find(user_id=user.id)) for user in users]
    assert len(users) == 4 and per_user == [1, 1, 1, 1]
    source = Path(identity.__file__).parent
    adding = sorted(
        str(path.relative_to(source)).replace("\\", "/")
        for path in source.rglob("*.py")
        if "memberships.add(" in path.read_text("utf-8")
    )
    assert adding == ["application/accounts.py", "application/users.py"]


async def _join(h: Harness, user_id: UUID, account_id: UUID, *, owner: bool = False) -> None:
    """A second membership, written straight into the store: no use case creates one."""
    async with h.store.transaction() as tx:
        await tx.memberships.add(
            Membership(uuid4(), account_id, user_id, Status.ACTIVE, h.clock(), is_owner=owner)
        )


def _signed_in(user_id: UUID, account_id: UUID, h: Harness) -> RequestContext:
    return RequestContext(
        principal=Principal(
            subject=user_id, account_id=account_id, kind=PrincipalKind.USER, auth_time=h.clock()
        )
    )


async def test_an_owner_of_another_account_is_not_deleted_as_a_member_here() -> None:
    """If a person ever owns account A and is a plain member of account B: "delete me" in B
    would delete the user and leave A without an owner, never purged. Refused, in the use case
    that decides and again in the transaction that deletes; nothing is removed."""
    async with harness() as h:
        lukas, bob = await h.register("lukas"), await h.register("bob")
        await _join(h, lukas.user_id, bob.account_id)
        before = _rows_of_user(h, lukas.user_id)
        in_b = _signed_in(lukas.user_id, bob.account_id, h)
        with pytest.raises(ConflictError) as decided:
            await h.app.state.identity.self_deletion.delete_me(PASSWORD, in_b)
        with pytest.raises(ConflictError) as in_transaction:
            await h.app.state.identity.users.delete_self(bob.account_id, in_b)
        # Signed in to the account they own: its deletion would erase the user, and with the
        # user the membership in the other account. Refused as well.
        as_owner = await _delete_me(h, lukas.token)
        directly = await h.client.request(
            "DELETE", lukas.url(), headers=lukas.headers, json={"password": PASSWORD}
        )
        me = (await h.client.get(ME, headers=lukas.headers)).json()["memberships"]
        account = (await h.client.get(lukas.url(), headers=lukas.headers)).json()
        events = h.bus.messages(DELETED)
    assert decided.value.message == OWNS_ANOTHER_ACCOUNT
    assert in_transaction.value.message == OWNS_ANOTHER_ACCOUNT
    assert as_owner.status_code == 409 and as_owner.json()["detail"] == MEMBER_OF_ANOTHER_ACCOUNT
    assert directly.status_code == 409 and directly.json()["detail"] == MEMBER_OF_ANOTHER_ACCOUNT
    assert [m["self_deletion"] for m in me] == [None, None]  # the client knows in advance
    assert [m["self_deletion_blocked"] for m in me] == [
        "member_of_another_account",
        "owns_another_account",
    ]
    assert _rows_of_user(h, lukas.user_id) == before and before["memberships"] == 2
    assert account["status"] == AccountStatus.ACTIVE.value and events == []


async def test_a_member_of_two_accounts_is_not_deleted_from_one() -> None:
    async with harness() as h:
        lukas, bob = await h.register("lukas"), await h.register("bob")
        anna_id, anna = await h.subuser(lukas, "anna")
        await _join(h, anna_id, bob.account_id)
        response = await _delete_me(h, anna)
        with pytest.raises(ConflictError) as in_transaction:
            await h.app.state.identity.users.delete_self(
                lukas.account_id, _signed_in(anna_id, lukas.account_id, h)
            )
    assert response.status_code == 409 and response.json()["detail"] == MEMBER_OF_ANOTHER_ACCOUNT
    assert in_transaction.value.message == MEMBER_OF_ANOTHER_ACCOUNT
    assert _rows_of_user(h, anna_id)["memberships"] == 2


# --- tokens ------------------------------------------------------------------------------------


async def test_personal_tokens_go_and_the_accounts_tokens_are_revoked() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        ada_id, ada = await _admin(h, lukas, "ada")
        level = {"type": "account", "id": str(lukas.account_id)}
        mine = await h.client.post(
            lukas.url("/api-tokens"),
            headers=lukas.headers,
            json={"name": "lukas ha", "grants": [{"level": level, "permissions": ["mower.view"]}]},
        )
        integration = await h.client.post(
            lukas.url("/api-tokens"),
            headers=bearer(ada),
            json={"name": "ada ha", "grants": [{"level": level, "permissions": ["mower.view"]}]},
        )
        personal = await h.client.post(
            f"{ME}/api-tokens",
            headers=bearer(ada),
            json={"name": "script", "scopes": ["mower.view"], "expires_in_days": 30},
        )
        widget = await h.client.post(
            f"{ME}/widget-credentials", headers=bearer(ada), json={"device_name": "Ada's iPhone"}
        )
        assert [r.status_code for r in (mine, integration, personal, widget)] == [201] * 4
        response = await _delete_me(h, ada)
        listed = (await h.client.get(lukas.url("/api-tokens"), headers=lukas.headers)).json()
        exchanges = [
            await h.client.post(f"{API}/token", json={"token": created.json()["token"]})
            for created in (integration, personal, widget, mine)
        ]
        revoked = {
            m.model(EventEnvelope[dict[str, Any]]).payload["token_id"]
            for m in h.bus.messages("event.identity.api_token.revoked")
        }
    assert response.status_code == 200
    by_name = {t["name"]: t for t in listed}
    assert set(by_name) == {"lukas ha", "ada ha"}  # the personal ones are gone, rows and all
    assert by_name["ada ha"]["revoked_at"] is not None and by_name["lukas ha"]["revoked_at"] is None
    assert [e.status_code for e in exchanges] == [401, 401, 401, 200]
    assert revoked == {integration.json()["id"], personal.json()["id"], widget.json()["id"]}
    for created in (integration, personal, widget):
        assert h.tuples_of(f"api_token:{created.json()['id']}") == set()
    assert h.tuples_of(f"api_token:{mine.json()['id']}")
    assert _rows_of_user(h, ada_id) == {}


# --- the audit log -----------------------------------------------------------------------------


async def test_the_audit_rows_stay_without_the_users_personal_fields() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        anna_id, anna = await h.subuser(lukas, "anna")
        await h.subuser(lukas, "ben")
        await h.client.patch(
            lukas.url(f"/users/{anna_id}"), headers=lukas.headers, json={"display_name": "Anna B."}
        )
        before = await h.audit(lukas, limit=200)
        response = await _delete_me(h, anna)
        after = await h.audit(lukas, limit=200)
        async with h.store.transaction() as tx:
            again = await tx.scrub_user_audit(anna_id)
    assert response.status_code == 200
    own = f"user:{anna_id}"
    about = [
        e
        for e in after
        if e["actor_subject"] == own or (e["target_type"], e["target_id"]) == ("user", str(anna_id))
    ]
    # Every row is still there, plus the deletion itself.
    assert len(after) == len(before) + 1
    assert after[0]["action"] == "user.self_deleted" and after[0]["details"] == {"subuser": True}
    assert {"user.created", "session.login", "user.renamed", "user.self_deleted"} <= {
        e["action"] for e in about
    }
    for entry in about:
        assert not {"username", "display_name", "device"} & set(entry["details"]), entry
        if entry["actor_subject"] in (own, None):  # the user's own name and address
            assert (entry["actor_name"], entry["ip"]) == (None, None), entry
        else:  # the admin who created or renamed the user keeps theirs
            assert entry["actor_subject"] == f"user:{lukas.user_id}"
            assert entry["actor_name"] == "lukas", entry
    # Rows about other people are untouched.
    others = [e for e in after if e not in about]
    assert any(e["details"].get("username") == "ben" for e in others)
    assert again == 0


# --- failure and the demo ----------------------------------------------------------------------


async def test_nothing_is_deleted_while_the_authorization_store_is_down() -> None:
    authz = InMemoryAuthorizer()
    backend = FlakyBackend(authz)
    async with harness(authz=authz, backend=backend) as h:
        lukas = await h.register("lukas")
        anna_id, anna = await h.subuser(lukas, "anna")
        before = _rows_of_user(h, anna_id)
        backend.fail_writes = True
        down = await _delete_me(h, anna)
        kept = _rows_of_user(h, anna_id)
        backend.fail_writes = False
        retried = await _delete_me(h, anna)
        events = h.bus.messages(DELETED)
    assert down.status_code == 503
    assert kept == before
    assert retried.status_code == 200 and _rows_of_user(h, anna_id) == {}
    assert len(events) == 1


async def test_the_demo_refuses_it() -> None:
    seed = "demo-seed-secret"
    async with harness(demo_mode=True, demo_seed_token=seed) as h:
        seeded = await h.client.post(f"{API}/demo/seed", headers={DEMO_SEED_HEADER: seed})
        token = (await h.client.post(f"{API}/demo/session")).json()["access_token"]
        response = await _delete_me(h, token)
        async with h.store.transaction() as tx:
            account = await tx.accounts.get(id=UUID(seeded.json()["account_id"]))
    assert response.status_code == 403
    assert response.json()["type"] == "urn:mowdeck:problem:demo-mode"
    assert account is not None and account.status is AccountStatus.ACTIVE


def _passkey(user_id: UUID, h: Harness) -> Passkey:
    return Passkey(
        id=uuid4(),
        user_id=user_id,
        credential_id=b"credential",
        public_key=b"key",
        sign_count=0,
        name="Anna's key",
        created_at=h.clock(),
    )


def _device(user_id: UUID, h: Harness) -> KnownDevice:
    return KnownDevice(
        id=uuid4(),
        user_id=user_id,
        token_hash="hash",  # noqa: S106 - not a secret
        label="Firefox on Windows",
        first_seen_at=h.clock(),
        last_seen_at=h.clock(),
    )
