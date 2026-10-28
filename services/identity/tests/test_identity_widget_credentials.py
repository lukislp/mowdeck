"""iOS widget credentials (ADR 0059): scoped API tokens that any user creates for themselves."""

from datetime import timedelta

from identity_support import API, harness
from mowdeck_auth.testing import bearer
from mowdeck_contracts.v1 import PrincipalKind

CREDENTIALS = f"{API}/me/widget-credentials"


async def test_owner_creates_a_scoped_credential_that_exchanges_for_a_short_access_token() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        created = await h.client.post(
            CREDENTIALS, headers=lukas.headers, json={"device_name": "Lukas' iPhone"}
        )
        body = created.json()
        exchanged = await h.client.post(f"{API}/token", json={"token": body["token"]})
        access = exchanged.json()["access_token"]
        me = (await h.client.get(f"{API}/me", headers=bearer(access))).json()
        tuples = h.tuples_of(f"api_token:{body['id']}")
        users = await h.client.get(lukas.url("/users"), headers=bearer(access))
        listed = (await h.client.get(CREDENTIALS, headers=lukas.headers)).json()
        in_token_list = (await h.client.get(lukas.url("/api-tokens"), headers=lukas.headers)).json()
    assert created.status_code == 201
    assert created.headers["cache-control"] == "no-store"
    assert body["name"] == "iOS widgets: Lukas' iPhone"
    assert body["token"].startswith("mdk_")
    assert body["permissions"] == ["mower.control", "mower.view"]
    assert exchanged.status_code == 200
    assert me["kind"] == PrincipalKind.API_TOKEN.value
    assert me["permissions"] == ["device_gateway.view", "mower.control", "mower.view"]
    assert tuples == {
        f"account:{lukas.account_id}#member",
        f"account:{lukas.account_id}#grant_mower_view",
        f"account:{lukas.account_id}#grant_mower_control",
    }
    assert users.status_code == 403  # no administrative rights
    assert [c["id"] for c in listed] == [body["id"]]
    assert all(c["token"] is None for c in listed)
    assert [t["id"] for t in in_token_list] == [body["id"]]  # visible to admins like any token


async def test_the_credential_lives_90_days() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        created = (
            await h.client.post(CREDENTIALS, headers=lukas.headers, json={"device_name": "iPad"})
        ).json()
        h.clock.advance(timedelta(days=89))
        before = await h.client.post(f"{API}/token", json={"token": created["token"]})
        h.clock.advance(timedelta(days=2))
        after = await h.client.post(f"{API}/token", json={"token": created["token"]})
    assert before.status_code == 200
    assert after.status_code == 401


async def test_a_viewer_gets_view_only_and_a_user_without_view_is_refused() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        viewer = await h.role_id(lukas, "viewer")
        level = {"type": "account", "id": str(lukas.account_id)}
        _, anna = await h.subuser(lukas, "anna", [{"role_id": str(viewer), "level": level}])
        _, ben = await h.subuser(lukas, "ben", [])
        view_only = await h.client.post(
            CREDENTIALS, headers=bearer(anna), json={"device_name": "Anna"}
        )
        nothing = await h.client.post(CREDENTIALS, headers=bearer(ben), json={"device_name": "Ben"})
    assert view_only.status_code == 201
    assert view_only.json()["permissions"] == ["mower.view"]
    assert nothing.status_code == 403


async def test_api_tokens_cannot_create_widget_credentials() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        created = (
            await h.client.post(CREDENTIALS, headers=lukas.headers, json={"device_name": "iPhone"})
        ).json()
        access = (await h.client.post(f"{API}/token", json={"token": created["token"]})).json()
        again = await h.client.post(
            CREDENTIALS, headers=bearer(access["access_token"]), json={"device_name": "evil"}
        )
        listed = await h.client.get(CREDENTIALS, headers=bearer(access["access_token"]))
        anonymous = await h.client.post(CREDENTIALS, json={"device_name": "x"})
    assert again.status_code == 403
    assert listed.json() == []  # created_by is the user, not the token
    assert anonymous.status_code == 401


async def test_rotation_revokes_the_replaced_credential_in_the_same_step() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        first = (
            await h.client.post(CREDENTIALS, headers=lukas.headers, json={"device_name": "iPhone"})
        ).json()
        second = await h.client.post(
            CREDENTIALS,
            headers=lukas.headers,
            json={"device_name": "iPhone", "replaces": first["id"]},
        )
        old = await h.client.post(f"{API}/token", json={"token": first["token"]})
        new = await h.client.post(f"{API}/token", json={"token": second.json()["token"]})
        tuples_old = h.tuples_of(f"api_token:{first['id']}")
        unknown = await h.client.post(
            CREDENTIALS,
            headers=lukas.headers,
            json={"device_name": "iPhone", "replaces": "00000000-0000-4000-8000-000000000000"},
        )
        entries = await h.audit(lukas, action="api_token.widget_created")
    assert second.status_code == 201
    assert old.status_code == 401
    assert new.status_code == 200
    assert tuples_old == set()
    assert unknown.status_code == 404
    assert len(entries) == 2


async def test_only_the_creator_revokes_a_widget_credential_and_only_widget_ones() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        manager = await h.role_id(lukas, "manager")
        level = {"type": "account", "id": str(lukas.account_id)}
        _, anna = await h.subuser(lukas, "anna", [{"role_id": str(manager), "level": level}])
        mine = (
            await h.client.post(CREDENTIALS, headers=lukas.headers, json={"device_name": "iPhone"})
        ).json()
        plain = (
            await h.client.post(
                lukas.url("/api-tokens"),
                headers=lukas.headers,
                json={
                    "name": "Home Assistant",
                    "grants": [{"level": level, "permissions": ["mower.view"]}],
                },
            )
        ).json()
        by_anna = await h.client.delete(f"{CREDENTIALS}/{mine['id']}", headers=bearer(anna))
        ordinary = await h.client.delete(f"{CREDENTIALS}/{plain['id']}", headers=lukas.headers)
        by_owner = await h.client.delete(f"{CREDENTIALS}/{mine['id']}", headers=lukas.headers)
        exchange = await h.client.post(f"{API}/token", json={"token": mine["token"]})
        again = await h.client.delete(f"{CREDENTIALS}/{mine['id']}", headers=lukas.headers)
        listed = (await h.client.get(CREDENTIALS, headers=lukas.headers)).json()
    assert by_anna.status_code == 404
    assert ordinary.status_code == 404
    assert by_owner.status_code == 204
    assert exchange.status_code == 401
    assert again.status_code == 204  # revoking twice is harmless
    assert listed[0]["revoked_at"] is not None


async def test_a_user_holds_at_most_ten_active_credentials() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        answers = [
            (
                await h.client.post(
                    CREDENTIALS, headers=lukas.headers, json={"device_name": f"iPhone {i}"}
                )
            ).status_code
            for i in range(11)
        ]
    assert answers == [201] * 10 + [409]


async def test_a_device_name_is_required() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        blank = await h.client.post(CREDENTIALS, headers=lukas.headers, json={"device_name": "   "})
        missing = await h.client.post(CREDENTIALS, headers=lukas.headers, json={})
    assert blank.status_code == 422
    assert missing.status_code == 422
