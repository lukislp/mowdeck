"""Personal access tokens (ADR 0064): a user's own API tokens for scripts.

Curated scopes, a required expiry of at most 365 days, step-up to create, hashed at rest, and
never usable to see or manage tokens."""

from datetime import timedelta
from uuid import UUID

from identity_support import API, PASSWORD, harness
from mowdeck_auth.testing import bearer
from mowdeck_contracts.v1 import PrincipalKind

TOKENS = f"{API}/me/api-tokens"
REAUTH = "urn:mowdeck:problem:reauth-required"


def _body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "name": "Garden script",
        "scopes": ["mower.view", "mower.control"],
        "expires_in_days": 30,
    }
    body.update(overrides)
    return body


async def test_a_user_creates_a_personal_token_that_exchanges_for_a_scoped_access_token() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        created = await h.client.post(TOKENS, headers=lukas.headers, json=_body())
        body = created.json()
        exchanged = await h.client.post(f"{API}/token", json={"token": body["token"]})
        access = exchanged.json()["access_token"]
        me = (await h.client.get(f"{API}/me", headers=bearer(access))).json()
        tuples = h.tuples_of(f"api_token:{body['id']}")
        listed = (await h.client.get(TOKENS, headers=lukas.headers)).json()
        admin_list = (await h.client.get(lukas.url("/api-tokens"), headers=lukas.headers)).json()
        entries = await h.audit(lukas, action="api_token.personal_created")
    assert created.status_code == 201
    assert created.headers["cache-control"] == "no-store"
    assert body["token"].startswith("mdk_")
    assert body["token"].startswith(body["prefix"])
    assert body["scopes"] == ["mower.control", "mower.view"]
    assert body["level"] == {"type": "account", "id": str(lukas.account_id)}
    assert body["expires_at"] is not None
    assert exchanged.status_code == 200
    assert me["kind"] == PrincipalKind.API_TOKEN.value
    assert tuples == {
        f"account:{lukas.account_id}#member",
        f"account:{lukas.account_id}#grant_mower_view",
        f"account:{lukas.account_id}#grant_mower_control",
    }
    # Listed without the secret, last use recorded by the exchange.
    assert [t["id"] for t in listed] == [body["id"]]
    assert listed[0]["token"] is None
    assert listed[0]["last_used_at"] is not None
    # Admins see personal tokens in the account's list, marked as such.
    assert [(t["id"], t["purpose"]) for t in admin_list] == [(body["id"], "personal")]
    assert len(entries) == 1
    assert body["token"] not in str(entries)


async def test_the_secret_is_stored_only_as_a_hash() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        body = (await h.client.post(TOKENS, headers=lukas.headers, json=_body())).json()
        async with h.app.state.identity.deps.store.transaction() as tx:
            stored = await tx.api_tokens.get(id=UUID(body["id"]))
    assert stored is not None
    assert body["token"] not in stored.secret_hash
    assert len(stored.secret_hash) == 64  # SHA-256 hex


async def test_the_token_expires_after_the_chosen_days() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        body = (
            await h.client.post(TOKENS, headers=lukas.headers, json=_body(expires_in_days=7))
        ).json()
        h.clock.advance(timedelta(days=6, hours=23))
        before = await h.client.post(f"{API}/token", json={"token": body["token"]})
        h.clock.advance(timedelta(hours=2))
        after = await h.client.post(f"{API}/token", json={"token": body["token"]})
    assert before.status_code == 200
    assert after.status_code == 401


async def test_expiry_and_scopes_are_validated() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        cases = {
            "no expiry": {k: v for k, v in _body().items() if k != "expires_in_days"},
            "too long": _body(expires_in_days=366),
            "zero days": _body(expires_in_days=0),
            "admin scope": _body(scopes=["mower.view", "users.manage"]),
            "drive scope": _body(scopes=["mower.view", "mower.drive"]),
            "devices scope": _body(scopes=["mower.view", "fleet.manage"]),
            "control without view": _body(scopes=["mower.control"]),
            "empty scopes": _body(scopes=[]),
            "blank name": _body(name="   "),
        }
        answers = {
            label: (await h.client.post(TOKENS, headers=lukas.headers, json=body)).status_code
            for label, body in cases.items()
        }
        year = await h.client.post(TOKENS, headers=lukas.headers, json=_body(expires_in_days=365))
        schedules = await h.client.post(
            TOKENS, headers=lukas.headers, json=_body(scopes=["mower.view", "schedule.edit"])
        )
    assert answers == dict.fromkeys(cases, 422)
    assert year.status_code == 201
    assert schedules.status_code == 201


async def test_creating_needs_a_recent_step_up() -> None:
    async with harness() as h:
        h.clock.advance(-timedelta(minutes=6))
        lukas = await h.register("lukas")
        h.clock.advance(timedelta(minutes=6))
        stale = await h.client.post(TOKENS, headers=lukas.headers, json=_body())
        step = await h.client.post(
            f"{API}/me/step-up/password", headers=lukas.headers, json={"password": PASSWORD}
        )
        fresh = await h.client.post(
            TOKENS, headers=bearer(step.json()["access_token"]), json=_body()
        )
    assert stale.status_code == 401
    assert stale.json()["type"] == REAUTH
    assert fresh.status_code == 201


async def test_tokens_never_see_or_manage_tokens() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        body = (await h.client.post(TOKENS, headers=lukas.headers, json=_body())).json()
        access = (await h.client.post(f"{API}/token", json={"token": body["token"]})).json()
        api_headers = bearer(access["access_token"])
        create = await h.client.post(TOKENS, headers=api_headers, json=_body())
        listed = await h.client.get(TOKENS, headers=api_headers)
        revoke = await h.client.delete(f"{TOKENS}/{body['id']}", headers=api_headers)
        admin = await h.client.get(lukas.url("/api-tokens"), headers=api_headers)
        anonymous = await h.client.get(TOKENS)
    assert create.status_code == 403
    assert listed.status_code == 403
    assert revoke.status_code == 403
    assert admin.status_code == 403
    assert anonymous.status_code == 401


async def test_scopes_are_capped_at_the_creators_rights() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        viewer = await h.role_id(lukas, "viewer")
        level = {"type": "account", "id": str(lukas.account_id)}
        _, anna = await h.subuser(lukas, "anna", [{"role_id": str(viewer), "level": level}])
        view = await h.client.post(TOKENS, headers=bearer(anna), json=_body(scopes=["mower.view"]))
        control = await h.client.post(TOKENS, headers=bearer(anna), json=_body())
    assert view.status_code == 201
    assert control.status_code == 403


async def test_only_the_creator_revokes_and_only_personal_tokens() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        manager = await h.role_id(lukas, "manager")
        level = {"type": "account", "id": str(lukas.account_id)}
        _, anna = await h.subuser(lukas, "anna", [{"role_id": str(manager), "level": level}])
        mine = (await h.client.post(TOKENS, headers=lukas.headers, json=_body())).json()
        integration = (
            await h.client.post(
                lukas.url("/api-tokens"),
                headers=lukas.headers,
                json={
                    "name": "Home Assistant",
                    "grants": [{"level": level, "permissions": ["mower.view"]}],
                },
            )
        ).json()
        anna_list = (await h.client.get(TOKENS, headers=bearer(anna))).json()
        by_anna = await h.client.delete(f"{TOKENS}/{mine['id']}", headers=bearer(anna))
        not_personal = await h.client.delete(f"{TOKENS}/{integration['id']}", headers=lukas.headers)
        own = await h.client.delete(f"{TOKENS}/{mine['id']}", headers=lukas.headers)
        again = await h.client.delete(f"{TOKENS}/{mine['id']}", headers=lukas.headers)
        exchanged = await h.client.post(f"{API}/token", json={"token": mine["token"]})
        listed = (await h.client.get(TOKENS, headers=lukas.headers)).json()
        tuples = h.tuples_of(f"api_token:{mine['id']}")
    assert anna_list == []
    assert by_anna.status_code == 404
    assert not_personal.status_code == 404
    assert own.status_code == 204
    assert again.status_code == 204  # idempotent
    assert exchanged.status_code == 401
    assert listed[0]["revoked_at"] is not None
    assert tuples == set()


async def test_at_most_25_active_personal_tokens() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        ids = []
        for number in range(25):
            created = await h.client.post(
                TOKENS, headers=lukas.headers, json=_body(name=f"script {number}")
            )
            ids.append(created.json()["id"])
        over = await h.client.post(TOKENS, headers=lukas.headers, json=_body(name="one more"))
        await h.client.delete(f"{TOKENS}/{ids[0]}", headers=lukas.headers)
        after_revoke = await h.client.post(
            TOKENS, headers=lukas.headers, json=_body(name="one more")
        )
    assert over.status_code == 409
    assert after_revoke.status_code == 201
