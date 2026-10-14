"""Plans in Identity (ADR 0028): the token's ``plan`` claim from Entitlements, ``max_users`` on
sub-user creation, and the operator level (``platform:main``): account list and CLI grant."""

from typing import Any
from uuid import UUID

import pytest
from identity_support import API, PASSWORD, harness, make_settings
from mowdeck_authz import PLATFORM_OBJECT, user
from mowdeck_authz.testing import InMemoryAuthorizer
from mowdeck_contracts.v1 import EventEnvelope, GrantsChanged, PlanLimits
from mowdeck_service_kit.testing import FakeEntitlements

from identity import cli
from identity.adapters.crypto import Argon2PasswordHasher
from identity.adapters.memory import MemoryStore
from identity.application.context import SYSTEM
from identity.domain.errors import NotFoundError
from identity.runtime import Runtime


def runtime_for(store: MemoryStore, authz: InMemoryAuthorizer) -> Runtime:
    return Runtime(
        make_settings(),
        store=store,
        authorizer=authz,
        tuple_backend=authz,
        hasher=Argon2PasswordHasher("insecure_test"),
    )


async def _me(h: Any, token: str) -> dict[str, Any]:
    response = await h.client.get(f"{API}/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_identity_plan_claim_comes_from_entitlements() -> None:
    plans = FakeEntitlements(plan="home")
    async with harness(entitlements=plans) as h:
        owner = await h.register("lukas")
        assert (await _me(h, owner.token))["plan"] == "home"
        plans.plan = "fleet"
        refreshed = await h.client.post(
            f"{API}/session/refresh", json={"refresh_token": owner.refresh_token}
        )
        assert refreshed.status_code == 200, refreshed.text
        assert (await _me(h, refreshed.json()["access_token"]))["plan"] == "fleet"


async def test_identity_plan_claim_is_unlimited_without_an_answer_and_billing_off() -> None:
    async with harness() as h:  # the real client, no Entitlements on the bus
        owner = await h.register("lukas")
        assert (await _me(h, owner.token))["plan"] == "unlimited"
    async with harness(entitlements_billing_enabled=True) as h:
        owner = await h.register("anna")
        assert (await _me(h, owner.token))["plan"] is None  # fails closed: unknown


async def test_identity_max_users_limits_sub_users() -> None:
    plans = FakeEntitlements(plan="home", limits=PlanLimits(max_users=2))
    async with harness(entitlements=plans) as h:
        owner = await h.register("lukas")
        await h.subuser(owner, "anna")
        refused = await h.client.post(
            owner.url("/users"),
            headers=owner.headers,
            json={"username": "bob", "password": PASSWORD, "assignments": []},
        )
        assert refused.status_code == 409
        problem = refused.json()
        assert problem["type"] == "urn:mowdeck:problem:plan-limit"
        assert {(e["code"], e["detail"]) for e in problem["errors"]} == {
            ("limit", "2"),
            ("current", "2"),
        }
        users = (await h.client.get(owner.url("/users"), headers=owner.headers)).json()
        assert sorted(u["username"] for u in users) == ["anna", "lukas"]
    assert ("limit", "max_users", 2) in plans.checks


async def test_identity_platform_accounts_need_an_operator_admin() -> None:
    async with harness() as h:
        lukas = await h.register("lukas", account_name="Lukas home")
        anna = await h.register("anna", account_name="Anna home")
        path = f"{API}/platform/accounts"
        assert (await h.client.get(path)).status_code == 401
        assert (await h.client.get(path, headers=lukas.headers)).status_code == 403
        services = h.app.state.identity
        await services.platform.set_admin("lukas", SYSTEM)
        assert f"operator_admin@user:{lukas.user_id}" in {
            f"{t.relation}@{t.subject}" for t in h.authz.tuples if t.object == PLATFORM_OBJECT
        }
        listed = await h.client.get(path, headers=lukas.headers, params={"limit": 1})
        assert listed.status_code == 200, listed.text
        page = listed.json()
        assert (page["total"], page["offset"], page["limit"]) == (2, 0, 1)
        [first] = page["items"]
        assert (first["name"], first["owner_username"], first["kind"]) == (
            "Lukas home",
            "lukas",
            "household",
        )
        assert set(first) == {"id", "name", "kind", "status", "created_at", "owner_username"}
        rest = await h.client.get(path, headers=lukas.headers, params={"offset": 1})
        assert [i["id"] for i in rest.json()["items"]] == [str(anna.account_id)]
        changed = [
            m.model(EventEnvelope[GrantsChanged]).payload
            for m in h.bus.messages("event.identity.grants.changed")
        ]
        assert any(f"user:{lukas.user_id}" in c.subjects for c in changed)
        await services.platform.set_admin("lukas", SYSTEM, grant=False)
        assert (await h.client.get(path, headers=lukas.headers)).status_code == 403
        with pytest.raises(NotFoundError):
            await services.platform.set_admin("nobody", SYSTEM)
        async with h.store.transaction() as tx:
            rows = await tx.audit.find()
        assert {"platform.admin_granted", "platform.admin_revoked"} <= {r.action for r in rows}


async def test_identity_cli_grants_platform_admin(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, authz = MemoryStore(), InMemoryAuthorizer()
    monkeypatch.setenv(cli.PASSWORD_ENV, PASSWORD)

    async def run(*argv: str) -> tuple[int, str, str]:
        code = await cli.amain(list(argv), runtime_for(store, authz))
        out = capsys.readouterr()
        return code, out.out, out.err

    code, out, _ = await run("create-user", "operator", "--account-name", "Operations")
    assert code == 0
    user_id = UUID(out.split()[-1])
    code, out, _ = await run("grant-platform-admin", "operator")
    assert code == 0 and "is now an operator admin" in out
    assert await authz.check(user(user_id), "can_manage_plans", PLATFORM_OBJECT)
    assert await authz.check(user(user_id), "can_manage_accounts", PLATFORM_OBJECT)
    code, out, _ = await run("grant-platform-admin", "operator", "--revoke")
    assert code == 0 and "is no longer" in out
    assert not await authz.check(user(user_id), "can_manage_plans", PLATFORM_OBJECT)
    code, _, err = await run("grant-platform-admin", "ghost")
    assert code == 1 and "user not found" in err


async def test_identity_me_platform_tells_whether_the_caller_is_an_operator_admin() -> None:
    async with harness() as h:
        lukas = await h.register("lukas", account_name="Lukas home")
        path = f"{API}/me/platform"
        assert (await h.client.get(path)).status_code == 401
        owner = await h.client.get(path, headers=lukas.headers)
        assert owner.status_code == 200, owner.text
        assert owner.json() == {"operator_admin": False}
        await h.app.state.identity.platform.set_admin("lukas", SYSTEM)
        admin = await h.client.get(path, headers=lukas.headers)
        assert admin.status_code == 200
        assert admin.json() == {"operator_admin": True}
