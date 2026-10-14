"""Sub-users, roles, assignments (with the tuples they write), API tokens, audit, deletion."""

from uuid import uuid4

from identity_support import API, PASSWORD, bearer, harness
from mowdeck_authz import group, mower, user
from mowdeck_contracts.v1 import PrincipalKind


async def test_subuser_with_operator_at_group_level_writes_the_expected_tuples() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        ids = await h.place(lukas.account_id)
        operator = await h.role_id(lukas, "operator")
        anna_id, anna_token = await h.subuser(
            lukas,
            "anna",
            [{"role_id": str(operator), "level": {"type": "group", "id": str(ids["north"])}}],
        )
        tuples = h.tuples_of(f"user:{anna_id}")
        anna = user(anna_id)
        m1 = await h.authz.check(anna, "can_control", mower(ids["m1"]))
        m2 = await h.authz.check(anna, "can_control", mower(ids["m2"]))
        me = (await h.client.get(f"{API}/me", headers=bearer(anna_token))).json()
        entries = await h.audit(lukas, action="role.assigned")
    north = f"group:{ids['north']}"
    assert tuples == {
        f"account:{lukas.account_id}#member",
        f"{north}#grant_mower_view",
        f"{north}#grant_mower_control",
        f"{north}#grant_mower_drive",
    }
    assert m1 and not m2
    assert me["kind"] == "subuser"
    assert me["roles"] == ["operator"]
    assert me["permissions"] == ["mower.control", "mower.drive", "mower.view"]
    assert "device_gateway.view" not in me["permissions"]  # implied only on the whole account
    assert entries[0]["target_id"] == str(anna_id)
    assert entries[0]["details"]["level"] == north
    assert entries[0]["correlation_id"]


async def test_operator_at_account_level_gets_implied_account_keys() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        operator = await h.role_id(lukas, "operator")
        _, token = await h.subuser(
            lukas,
            "anna",
            [{"role_id": str(operator), "level": {"type": "account", "id": str(lukas.account_id)}}],
        )
        me = (await h.client.get(f"{API}/me", headers=bearer(token))).json()
        users = await h.client.get(lukas.url("/users"), headers=bearer(token))
        own = await h.client.get(
            lukas.url("/role-assignments"),
            headers=bearer(token),
            params={"subject_id": me["subject"]},
        )
        others = await h.client.get(lukas.url("/role-assignments"), headers=bearer(token))
    assert "device_gateway.view" in me["permissions"]
    assert users.status_code == 403  # a member without users.manage
    assert own.status_code == 200 and len(own.json()) == 1
    assert others.status_code == 403


async def test_disable_cascades_and_enable_restores() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        viewer = await h.role_id(lukas, "viewer")
        manager = await h.role_id(lukas, "manager")
        level = {"type": "account", "id": str(lukas.account_id)}
        anna_id, anna_token = await h.subuser(
            lukas,
            "anna",
            [
                {"role_id": str(viewer), "level": level},
                {"role_id": str(manager), "level": level},
            ],
        )
        anna_login = await h.login("anna")
        token = await h.client.post(
            lukas.url("/api-tokens"),
            headers=bearer(anna_token),
            json={"name": "ha", "grants": [{"level": level, "permissions": ["mower.view"]}]},
        )
        assert token.status_code == 403  # anna lacks users.manage
        # Tokens are created by their user: give anna users.manage through a custom role.
        admin_role = await h.client.post(
            lukas.url("/roles"),
            headers=lukas.headers,
            json={"name": "Token keeper", "permissions": ["users.manage"]},
        )
        assert admin_role.status_code == 201
        await h.client.post(
            lukas.url("/role-assignments"),
            headers=lukas.headers,
            json={"subject_id": str(anna_id), "role_id": admin_role.json()["id"], "level": level},
        )
        anna_token = (await h.login("anna"))["access_token"]
        created = await h.client.post(
            lukas.url("/api-tokens"),
            headers=bearer(anna_token),
            json={"name": "ha", "grants": [{"level": level, "permissions": ["mower.view"]}]},
        )
        assert created.status_code == 201, created.text
        token_id = created.json()["id"]
        disabled = await h.client.post(
            lukas.url(f"/users/{anna_id}/disable"), headers=lukas.headers
        )
        refresh = await h.client.post(
            f"{API}/session/refresh", json={"refresh_token": anna_login["refresh_token"]}
        )
        login = await h.client.post(
            f"{API}/session", json={"username": "anna", "password": PASSWORD}
        )
        exchange = await h.client.post(f"{API}/token", json={"token": created.json()["token"]})
        tuples_disabled = h.tuples_of(f"user:{anna_id}") | h.tuples_of(f"api_token:{token_id}")
        events = [m.subject for m in h.bus.published]
        enabled = await h.client.post(lukas.url(f"/users/{anna_id}/enable"), headers=lukas.headers)
        tuples_enabled = h.tuples_of(f"user:{anna_id}")
        tokens = (await h.client.get(lukas.url("/api-tokens"), headers=lukas.headers)).json()
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "disabled"
    assert refresh.status_code == 401
    assert login.status_code == 401
    assert exchange.status_code == 401
    assert tuples_disabled == set()
    assert "event.identity.user.disabled" in events
    assert "event.identity.api_token.revoked" in events
    assert enabled.json()["status"] == "active"
    assert f"account:{lukas.account_id}#grant_fleet_manage" in tuples_enabled
    assert tokens[0]["revoked_at"] is not None  # revoked tokens stay revoked


async def test_owner_and_admin_guards() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        level = {"type": "account", "id": str(lukas.account_id)}
        admin = await h.role_id(lukas, "admin")
        owner = await h.role_id(lukas, "owner")
        ada_id, ada_token = await h.subuser(lukas, "ada", [{"role_id": str(admin), "level": level}])
        ada_token = (await h.login("ada"))["access_token"]
        ben_id, _ = await h.subuser(lukas, "ben")
        ada_tuples = h.tuples_of(f"user:{ada_id}")
        disable_owner = await h.client.post(
            lukas.url(f"/users/{lukas.user_id}/disable"), headers=bearer(ada_token)
        )
        self_disable = await h.client.post(
            lukas.url(f"/users/{lukas.user_id}/disable"), headers=lukas.headers
        )
        assign_owner = await h.client.post(
            lukas.url("/role-assignments"),
            headers=lukas.headers,
            json={"subject_id": str(ben_id), "role_id": str(owner), "level": level},
        )
        admin_by_admin = await h.client.post(
            lukas.url("/role-assignments"),
            headers=bearer(ada_token),
            json={"subject_id": str(ben_id), "role_id": str(admin), "level": level},
        )
        users = await h.client.get(lukas.url("/users"), headers=bearer(ada_token))
        ada_assignment = next(
            a["id"]
            for a in (
                await h.client.get(lukas.url("/role-assignments"), headers=lukas.headers)
            ).json()
            if a["subject_id"] == str(ada_id)
        )
        unassign_by_admin = await h.client.delete(
            lukas.url(f"/role-assignments/{ada_assignment}"), headers=bearer(ada_token)
        )
        remove_admin_by_admin = await h.client.delete(
            lukas.url(f"/users/{ada_id}"), headers=bearer(ada_token)
        )
        unassign = await h.client.delete(
            lukas.url(f"/role-assignments/{ada_assignment}"), headers=lukas.headers
        )
        ada_after = h.tuples_of(f"user:{ada_id}")
    assert f"account:{lukas.account_id}#admin" in ada_tuples
    assert disable_owner.status_code == 403
    assert self_disable.status_code == 422
    assert assign_owner.status_code == 403
    assert admin_by_admin.status_code == 403
    assert users.status_code == 200  # admin manages users
    assert unassign_by_admin.status_code == 403
    assert remove_admin_by_admin.status_code == 422  # removing yourself is refused first
    assert unassign.status_code == 204
    assert ada_after == {f"account:{lukas.account_id}#member"}


async def test_anti_escalation_for_roles_assignments_and_tokens() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        ids = await h.place(lukas.account_id)
        north = {"type": "group", "id": str(ids["north"])}
        keeper = await h.client.post(
            lukas.url("/roles"),
            headers=lukas.headers,
            json={
                "name": "Group lead",
                "permissions": ["users.manage", "mower.view", "mower.control"],
            },
        )
        level = {"type": "account", "id": str(lukas.account_id)}
        operator = await h.role_id(lukas, "operator")
        manager = await h.role_id(lukas, "manager")
        _, lead_token = await h.subuser(
            lukas,
            "lead",
            [
                {"role_id": keeper.json()["id"], "level": level},
                {"role_id": str(operator), "level": north},
            ],
        )
        ben_id, _ = await h.subuser(lukas, "ben")
        lead = bearer(lead_token)
        ok_operator = await h.client.post(
            lukas.url("/role-assignments"),
            headers=lead,
            json={"subject_id": str(ben_id), "role_id": str(operator), "level": north},
        )
        too_much_manager = await h.client.post(
            lukas.url("/role-assignments"),
            headers=lead,
            json={"subject_id": str(ben_id), "role_id": str(manager), "level": north},
        )
        operator_on_account = await h.client.post(
            lukas.url("/role-assignments"),
            headers=lead,
            json={"subject_id": str(ben_id), "role_id": str(operator), "level": level},
        )
        bigger_role = await h.client.post(
            lukas.url("/roles"),
            headers=lead,
            json={"name": "Big", "permissions": ["mower.view", "fleet.manage"]},
        )
        blind_role = await h.client.post(
            lukas.url("/roles"),
            headers=lukas.headers,
            json={"name": "Blind", "permissions": ["mower.control"]},
        )
        unknown_key = await h.client.post(
            lukas.url("/roles"),
            headers=lukas.headers,
            json={"name": "X", "permissions": ["mower.fly"]},
        )
        admin_token = await h.client.post(
            lukas.url("/api-tokens"),
            headers=lukas.headers,
            json={"name": "x", "grants": [{"level": level, "permissions": ["users.manage"]}]},
        )
        token_beyond = await h.client.post(
            lukas.url("/api-tokens"),
            headers=lead,
            json={
                "name": "x",
                "grants": [
                    {"level": north, "permissions": ["mower.drive", "mower.view"]},
                    {"level": level, "permissions": ["mower.drive"]},
                ],
            },
        )
        token_inside = await h.client.post(
            lukas.url("/api-tokens"),
            headers=lead,
            json={
                "name": "ha",
                "grants": [{"level": north, "permissions": ["mower.view", "mower.drive"]}],
            },
        )
    assert keeper.status_code == 201
    assert ok_operator.status_code == 201
    assert too_much_manager.status_code == 403
    assert operator_on_account.status_code == 403  # lead drives only in north
    assert bigger_role.status_code == 403
    assert blind_role.status_code == 422  # control without view
    assert unknown_key.status_code == 422
    assert admin_token.status_code == 422  # users.manage can never go to a token
    assert token_beyond.status_code == 403
    assert token_inside.status_code == 201


async def test_api_token_exchange_yields_a_capped_api_token_principal() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        ids = await h.place(lukas.account_id)
        level = {"type": "account", "id": str(lukas.account_id)}
        created = await h.client.post(
            lukas.url("/api-tokens"),
            headers=lukas.headers,
            json={
                "name": "Home Assistant",
                "grants": [
                    {"level": level, "permissions": ["mower.view"]},
                    {
                        "level": {"type": "mower", "id": str(ids["m1"])},
                        "permissions": ["mower.control"],
                    },
                ],
            },
        )
        secret = created.json()["token"]
        token_id = created.json()["id"]
        exchanged = await h.client.post(
            f"{API}/token", json={"grant_type": "api_token", "token": secret}
        )
        access = exchanged.json()["access_token"]
        me = (await h.client.get(f"{API}/me", headers=bearer(access))).json()
        tuples = h.tuples_of(f"api_token:{token_id}")
        users = await h.client.get(lukas.url("/users"), headers=bearer(access))
        listed = (await h.client.get(lukas.url("/api-tokens"), headers=lukas.headers)).json()
        bad = await h.client.post(f"{API}/token", json={"token": secret[:-2] + "xx"})
        malformed = await h.client.post(f"{API}/token", json={"token": "nope"})
        revoke = await h.client.delete(lukas.url(f"/api-tokens/{token_id}"), headers=lukas.headers)
        after = await h.client.post(f"{API}/token", json={"token": secret})
        tuples_after = h.tuples_of(f"api_token:{token_id}")
        entries = await h.audit(lukas, target_id=token_id)
    assert created.status_code == 201
    assert secret.startswith("mdk_")
    assert exchanged.status_code == 200
    assert me["kind"] == PrincipalKind.API_TOKEN.value
    assert me["permissions"] == ["device_gateway.view", "mower.control", "mower.view"]
    assert me["roles"] == []
    assert me["session_id"] is None
    assert tuples == {
        f"account:{lukas.account_id}#member",
        f"account:{lukas.account_id}#grant_mower_view",
        f"mower:{ids['m1']}#grant_mower_control",
    }
    assert users.status_code == 403
    assert "token" not in listed[0] or listed[0]["token"] is None
    assert bad.status_code == malformed.status_code == 401
    assert revoke.status_code == 204
    assert after.status_code == 401
    assert tuples_after == set()
    assert {"api_token.created", "api_token.exchanged", "api_token.revoked"} <= {
        e["action"] for e in entries
    }


async def test_role_edit_reconciles_holders_and_shared_tuples_survive() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        level = {"type": "account", "id": str(lukas.account_id)}
        viewer = await h.role_id(lukas, "viewer")
        custom = await h.client.post(
            lukas.url("/roles"),
            headers=lukas.headers,
            json={"name": "Watcher", "permissions": ["mower.view"]},
        )
        anna_id, _ = await h.subuser(
            lukas,
            "anna",
            [
                {"role_id": str(viewer), "level": level},
                {"role_id": custom.json()["id"], "level": level},
            ],
        )
        role_id = custom.json()["id"]
        grown = await h.client.patch(
            lukas.url(f"/roles/{role_id}"),
            headers=lukas.headers,
            json={"permissions": ["mower.view", "schedule.edit"], "name": "Watcher plus"},
        )
        after_grow = h.tuples_of(f"user:{anna_id}")
        shrunk = await h.client.patch(
            lukas.url(f"/roles/{role_id}"), headers=lukas.headers, json={"permissions": []}
        )
        after_shrink = h.tuples_of(f"user:{anna_id}")
        delete_assigned = await h.client.delete(
            lukas.url(f"/roles/{role_id}"), headers=lukas.headers
        )
        system = await h.client.patch(
            lukas.url(f"/roles/{viewer}"), headers=lukas.headers, json={"permissions": []}
        )
        owner = await h.role_id(lukas, "owner")
        owner_edit = await h.client.patch(
            lukas.url(f"/roles/{owner}"), headers=lukas.headers, json={"name": "Boss"}
        )
        owner_delete = await h.client.delete(lukas.url(f"/roles/{owner}"), headers=lukas.headers)
        unused = await h.client.post(
            lukas.url("/roles"), headers=lukas.headers, json={"name": "Spare", "permissions": []}
        )
        duplicate = await h.client.post(
            lukas.url("/roles"), headers=lukas.headers, json={"name": "Spare", "permissions": []}
        )
        deleted = await h.client.delete(
            lukas.url(f"/roles/{unused.json()['id']}"), headers=lukas.headers
        )
        missing = await h.client.delete(lukas.url(f"/roles/{uuid4()}"), headers=lukas.headers)
        roles = (await h.client.get(lukas.url("/roles"), headers=lukas.headers)).json()
    acc = f"account:{lukas.account_id}"
    assert grown.status_code == 200 and grown.json()["name"] == "Watcher plus"
    assert f"{acc}#grant_schedule_edit" in after_grow
    assert after_shrink == {f"{acc}#member", f"{acc}#grant_mower_view"}  # viewer still grants view
    assert shrunk.status_code == 200
    assert delete_assigned.status_code == 409
    assert system.status_code == 200  # default roles are editable bundles
    assert owner_edit.status_code == 403
    assert owner_delete.status_code == 403
    assert duplicate.status_code == 409
    assert deleted.status_code == 204
    assert missing.status_code == 404
    assert [r["key"] for r in roles][:2] == ["owner", "admin"]


async def test_users_crud_password_and_removal() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        anna_id, anna_token = await h.subuser(lukas, "anna")
        listed = (await h.client.get(lukas.url("/users"), headers=lukas.headers)).json()
        own = await h.client.get(lukas.url(f"/users/{anna_id}"), headers=bearer(anna_token))
        other = await h.client.get(lukas.url(f"/users/{lukas.user_id}"), headers=bearer(anna_token))
        renamed = await h.client.patch(
            lukas.url(f"/users/{anna_id}"), headers=lukas.headers, json={"display_name": "Anna S."}
        )
        rename_owner = await h.client.patch(
            lukas.url(f"/users/{lukas.user_id}"), headers=lukas.headers, json={"display_name": "Me"}
        )
        reset = await h.client.post(
            lukas.url(f"/users/{anna_id}/password"),
            headers=lukas.headers,
            json={"password": "temporary password"},
        )
        reset_owner = await h.client.post(
            lukas.url(f"/users/{lukas.user_id}/password"),
            headers=lukas.headers,
            json={"password": "temporary password"},
        )
        relogin = await h.login("anna", "temporary password")
        me = (await h.client.get(f"{API}/me", headers=bearer(relogin["access_token"]))).json()
        taken = await h.client.post(
            lukas.url("/users"),
            headers=lukas.headers,
            json={"username": "ANNA", "password": PASSWORD},
        )
        removed = await h.client.delete(lukas.url(f"/users/{anna_id}"), headers=lukas.headers)
        gone = await h.client.get(lukas.url(f"/users/{anna_id}"), headers=lukas.headers)
        login = await h.client.post(
            f"{API}/session", json={"username": "anna", "password": "temporary password"}
        )
        events = [m.subject for m in h.bus.published]
    assert {u["username"] for u in listed} == {"lukas", "anna"}
    assert own.status_code == 200
    assert other.status_code == 403
    assert renamed.json()["display_name"] == "Anna S."
    assert rename_owner.status_code == 200  # renaming yourself is fine
    assert reset.status_code == 204
    assert reset_owner.status_code == 403
    assert me["must_change_password"] is True
    assert taken.status_code == 409
    assert removed.status_code == 204
    assert gone.status_code == 404
    assert login.status_code == 401
    assert h.tuples_of(f"user:{anna_id}") == set()
    assert "event.identity.user.deleted" in events


async def test_cross_account_access_is_hidden_or_forbidden() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        ids = await h.place(lukas.account_id)
        anna_id, _ = await h.subuser(lukas, "anna")
        token = await h.client.post(
            lukas.url("/api-tokens"),
            headers=lukas.headers,
            json={
                "name": "ha",
                "grants": [
                    {
                        "level": {"type": "account", "id": str(lukas.account_id)},
                        "permissions": ["mower.view"],
                    }
                ],
            },
        )
        role = await h.role_id(lukas, "viewer")
        bob = await h.register("bob")
        b = bob.headers
        a = lukas
        attempts = {
            "account": await h.client.get(a.url(), headers=b),
            "users": await h.client.get(a.url("/users"), headers=b),
            "user": await h.client.get(a.url(f"/users/{anna_id}"), headers=b),
            "patch_user": await h.client.patch(
                a.url(f"/users/{anna_id}"), headers=b, json={"display_name": "x"}
            ),
            "delete_user": await h.client.delete(a.url(f"/users/{anna_id}"), headers=b),
            "disable_user": await h.client.post(a.url(f"/users/{anna_id}/disable"), headers=b),
            "create_user": await h.client.post(
                a.url("/users"), headers=b, json={"username": "mallory", "password": PASSWORD}
            ),
            "roles": await h.client.get(a.url("/roles"), headers=b),
            "create_role": await h.client.post(
                a.url("/roles"), headers=b, json={"name": "x", "permissions": []}
            ),
            "assignments": await h.client.get(a.url("/role-assignments"), headers=b),
            "tokens": await h.client.get(a.url("/api-tokens"), headers=b),
            "revoke_token": await h.client.delete(
                a.url(f"/api-tokens/{token.json()['id']}"), headers=b
            ),
            "audit": await h.client.get(a.url("/audit"), headers=b),
            "rename": await h.client.patch(a.url(), headers=b, json={"name": "mine now"}),
            "delete": await h.client.request(
                "DELETE", a.url(), headers=b, json={"password": PASSWORD}
            ),
            "reconcile": await h.client.post(a.url("/reconcile"), headers=b),
            "permissions": await h.client.get(a.url("/permissions"), headers=b),
        }
        # Bob in his own account, pointing at Lukas' objects:
        own_role = await h.role_id(bob, "operator")
        bob_sub, _ = await h.subuser(bob, "bobsub")
        grant_on_a_group = await h.client.post(
            bob.url("/role-assignments"),
            headers=b,
            json={
                "subject_id": str(bob_sub),
                "role_id": str(own_role),
                "level": {"type": "group", "id": str(ids["north"])},
            },
        )
        assign_a_user = await h.client.post(
            bob.url("/role-assignments"),
            headers=b,
            json={
                "subject_id": str(anna_id),
                "role_id": str(own_role),
                "level": {"type": "account", "id": str(bob.account_id)},
            },
        )
        foreign_role = await h.client.post(
            bob.url("/role-assignments"),
            headers=b,
            json={
                "subject_id": str(bob_sub),
                "role_id": str(role),
                "level": {"type": "account", "id": str(bob.account_id)},
            },
        )
        foreign_level = await h.client.post(
            bob.url("/role-assignments"),
            headers=b,
            json={
                "subject_id": str(bob_sub),
                "role_id": str(own_role),
                "level": {"type": "account", "id": str(lukas.account_id)},
            },
        )
        foreign_token = await h.client.delete(
            bob.url(f"/api-tokens/{token.json()['id']}"), headers=b
        )
        bob_users = (await h.client.get(bob.url("/users"), headers=b)).json()
        bob_audit = await h.audit(bob)
        north_for_bob = await h.authz.check(user(bob.user_id), "can_view", group(ids["north"]))
    assert {name: r.status_code for name, r in attempts.items()} == dict.fromkeys(attempts, 404)
    assert grant_on_a_group.status_code == 404
    assert assign_a_user.status_code == 404
    assert foreign_role.status_code == 404
    assert foreign_level.status_code == 404
    assert foreign_token.status_code == 404
    assert {u["username"] for u in bob_users} == {"bob", "bobsub"}
    assert all(e["details"].get("username") != "lukas" for e in bob_audit)
    assert not north_for_bob


async def test_member_without_rights_gets_403_not_404() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        _, anna_token = await h.subuser(lukas, "anna")
        a = bearer(anna_token)
        account = await h.client.get(lukas.url(), headers=a)
        roles = await h.client.get(lukas.url("/roles"), headers=a)
        users = await h.client.get(lukas.url("/users"), headers=a)
        audit = await h.client.get(lukas.url("/audit"), headers=a)
        delete = await h.client.request(
            "DELETE", lukas.url(), headers=a, json={"password": PASSWORD}
        )
        bad_path = await h.client.get(f"{API}/accounts/not-a-uuid/users", headers=a)
    assert account.status_code == 200
    assert roles.status_code == 200
    assert users.status_code == audit.status_code == delete.status_code == 403
    assert bad_path.status_code == 404


async def test_audit_query_filters_and_pagination() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        for name in ("user1", "user2", "user3"):
            await h.subuser(lukas, name)
        first = await h.audit(lukas, action="user.created", limit=2)
        second = await h.audit(lukas, action="user.created", before_id=first[-1]["id"])
        by_actor = await h.audit(lukas, actor=f"user:{lukas.user_id}", action="user.created")
    assert len(first) == 2 and len(second) == 1
    assert first[0]["id"] > first[1]["id"] > second[0]["id"]
    assert len(by_actor) == 3


async def test_account_rename_delete_with_grace_and_restore() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        operator = await h.role_id(lukas, "operator")
        anna_id, _ = await h.subuser(
            lukas,
            "anna",
            [{"role_id": str(operator), "level": {"type": "account", "id": str(lukas.account_id)}}],
        )
        anna_login = await h.login("anna")
        renamed = await h.client.patch(
            lukas.url(), headers=lukas.headers, json={"name": "New name"}
        )
        wrong = await h.client.request(
            "DELETE", lukas.url(), headers=lukas.headers, json={"password": "wrong password"}
        )
        deleted = await h.client.request(
            "DELETE", lukas.url(), headers=lukas.headers, json={"password": PASSWORD}
        )
        twice = await h.client.request(
            "DELETE", lukas.url(), headers=lukas.headers, json={"password": PASSWORD}
        )
        anna_refresh = await h.client.post(
            f"{API}/session/refresh", json={"refresh_token": anna_login["refresh_token"]}
        )
        anna_relogin = await h.client.post(
            f"{API}/session", json={"username": "anna", "password": PASSWORD}
        )
        anna_tuples = h.tuples_of(f"user:{anna_id}")
        owner_tuples = h.tuples_of(f"user:{lukas.user_id}")
        owner_login = await h.login("lukas")
        restored = await h.client.post(
            lukas.url("/restore"), headers=bearer(owner_login["access_token"])
        )
        restore_again = await h.client.post(
            lukas.url("/restore"), headers=bearer(owner_login["access_token"])
        )
        anna_back = h.tuples_of(f"user:{anna_id}")
    assert renamed.json()["name"] == "New name"
    assert wrong.status_code == 401
    assert deleted.status_code == 202
    assert deleted.json()["status"] == "deleting" and deleted.json()["delete_after"]
    assert twice.status_code == 409
    assert anna_refresh.status_code == 401
    assert anna_relogin.status_code == 401
    assert anna_tuples == set()
    assert owner_tuples == {
        f"account:{lukas.account_id}#member",
        f"account:{lukas.account_id}#owner",
    }
    assert restored.json()["status"] == "active"
    assert restore_again.status_code == 409
    assert f"account:{lukas.account_id}#grant_mower_control" in anna_back


async def test_reconcile_repairs_drift_and_catalogue_is_served() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        operator = await h.role_id(lukas, "operator")
        anna_id, _ = await h.subuser(
            lukas,
            "anna",
            [{"role_id": str(operator), "level": {"type": "account", "id": str(lukas.account_id)}}],
        )
        for t in [t for t in h.authz.tuples if str(t.subject) == f"user:{anna_id}"]:
            h.authz.remove(t)
        repaired = await h.client.post(lukas.url("/reconcile"), headers=lukas.headers)
        catalogue = (await h.client.get(lukas.url("/permissions"), headers=lukas.headers)).json()
        checks = await h.client.post(
            f"{API}/me/permissions",
            headers=lukas.headers,
            json={
                "checks": [
                    {"relation": "can_manage_users", "object": f"account:{lukas.account_id}"},
                    {"relation": "can_view", "object": f"account:{uuid4()}"},
                ]
            },
        )
        empty = await h.client.post(
            f"{API}/me/permissions", headers=lukas.headers, json={"checks": []}
        )
    assert repaired.status_code == 200
    assert repaired.json()["changed_subjects"] == [f"user:{anna_id}"]
    assert repaired.json()["writes"] == 4
    keys = {p["key"]: p for p in catalogue}
    assert keys["device_gateway.view"]["levels"] == ["account"]
    assert keys["device_gateway.view"]["implied_by"] == ["mower.view"]
    assert keys["users.manage"]["api_tokens"] is False
    assert keys["mower.view"]["levels"] == ["account", "site", "group", "mower"]
    assert checks.json() == [True, False]
    assert empty.json() == []
