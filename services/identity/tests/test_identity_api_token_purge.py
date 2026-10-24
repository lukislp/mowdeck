"""Deleting revoked API tokens for good: one (``DELETE ...?purge=true``) or all of the account
(``POST .../api-tokens/purge-revoked``). Revoke first, ``users.manage``, account isolation,
audit by name and prefix, the audit history of the token stays, no tuple is left behind."""

from typing import Any
from uuid import UUID, uuid4

import pytest
from identity_support import API, Account, Harness, bearer, harness
from mowdeck_authz import RelationshipTuple, account, api_token
from mowdeck_contracts.v1 import Principal, PrincipalKind

from identity.application.context import RequestContext
from identity.domain.errors import ConflictError, NotFoundError


async def make_token(
    h: Harness, owner: Account, name: str, *, revoke: bool = False
) -> dict[str, Any]:
    """Create a view-only token on the whole account (and revoke it); returns the create answer."""
    created = await h.client.post(
        owner.url("/api-tokens"),
        headers=owner.headers,
        json={
            "name": name,
            "grants": [
                {
                    "level": {"type": "account", "id": str(owner.account_id)},
                    "permissions": ["mower.view"],
                }
            ],
        },
    )
    assert created.status_code == 201, created.text
    body: dict[str, Any] = created.json()
    if revoke:
        revoked = await h.client.delete(
            owner.url(f"/api-tokens/{body['id']}"), headers=owner.headers
        )
        assert revoked.status_code == 204, revoked.text
    return body


async def listed_names(h: Harness, owner: Account) -> list[str]:
    response = await h.client.get(owner.url("/api-tokens"), headers=owner.headers)
    assert response.status_code == 200, response.text
    return sorted(t["name"] for t in response.json())


def owner_ctx(owner: Account) -> RequestContext:
    return RequestContext(principal=Principal(subject=owner.user_id, kind=PrincipalKind.USER))


async def test_a_revoked_token_is_deleted_for_good_and_its_audit_history_stays() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        old = await make_token(h, lukas, "Altes Tablet", revoke=True)
        keep = await make_token(h, lukas, "Home Assistant")
        purge = await h.client.delete(
            lukas.url(f"/api-tokens/{old['id']}"), headers=lukas.headers, params={"purge": "true"}
        )
        again = await h.client.delete(
            lukas.url(f"/api-tokens/{old['id']}"), headers=lukas.headers, params={"purge": "true"}
        )
        names = await listed_names(h, lukas)
        entries = await h.audit(lukas, target_id=old["id"])
        exchange = await h.client.post(f"{API}/token", json={"token": old["token"]})
        tuples = h.tuples_of(f"api_token:{old['id']}")
        keep_tuples = h.tuples_of(f"api_token:{keep['id']}")
    assert purge.status_code == 204
    assert again.status_code == 404  # gone for good
    assert names == ["Home Assistant"]
    actions = [e["action"] for e in entries]
    # Newest first; the history of the token stays next to the new row.
    assert actions[0] == "api_token.purged"
    assert {"api_token.created", "api_token.revoked"} <= set(actions)
    purged = entries[0]
    assert purged["details"]["name"] == "Altes Tablet"
    assert purged["details"]["prefix"] == old["prefix"]
    assert purged["details"]["bulk"] is False
    assert purged["details"]["revoke_reason"] == "revoked"
    assert purged["actor_subject"] == f"user:{lukas.user_id}"
    assert old["token"] not in str(purged)  # never the secret
    assert exchange.status_code == 401
    assert tuples == set()
    assert keep_tuples  # the active token is untouched


async def test_an_active_token_cannot_be_purged_revoke_first() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        token = await make_token(h, lukas, "Home Assistant")
        refused = await h.client.delete(
            lukas.url(f"/api-tokens/{token['id']}"), headers=lukas.headers, params={"purge": "true"}
        )
        still = await listed_names(h, lukas)
        exchange = await h.client.post(f"{API}/token", json={"token": token["token"]})
        entries = await h.audit(lukas, target_id=token["id"])
        with pytest.raises(ConflictError, match="revoke"):
            await h.app.state.identity.tokens.purge(
                lukas.account_id, UUID(token["id"]), owner_ctx(lukas)
            )
    assert refused.status_code == 409
    assert "revoke" in refused.json()["detail"]
    assert still == ["Home Assistant"]
    assert exchange.status_code == 200  # still works: nothing was revoked on the way
    assert "api_token.purged" not in {e["action"] for e in entries}
    assert "api_token.revoked" not in {e["action"] for e in entries}


async def test_purge_revoked_deletes_every_revoked_token_and_keeps_the_active_ones() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        first = await make_token(h, lukas, "Testskript", revoke=True)
        second = await make_token(h, lukas, "Altes Handy", revoke=True)
        active = await make_token(h, lukas, "Home Assistant")
        bulk = await h.client.post(lukas.url("/api-tokens/purge-revoked"), headers=lukas.headers)
        empty = await h.client.post(lukas.url("/api-tokens/purge-revoked"), headers=lukas.headers)
        names = await listed_names(h, lukas)
        purged = await h.audit(lukas, action="api_token.purged")
        exchange = await h.client.post(f"{API}/token", json={"token": active["token"]})
    assert bulk.status_code == 200
    body = bulk.json()
    assert body["deleted"] == 2
    assert sorted(body["ids"]) == sorted([first["id"], second["id"]])
    assert empty.json() == {"deleted": 0, "ids": []}
    assert names == ["Home Assistant"]
    assert sorted(e["target_id"] for e in purged) == sorted([first["id"], second["id"]])
    assert {e["details"]["name"] for e in purged} == {"Testskript", "Altes Handy"}
    assert all(e["details"]["bulk"] is True for e in purged)
    assert exchange.status_code == 200


async def test_purging_needs_users_manage() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        token = await make_token(h, lukas, "Altes Tablet", revoke=True)
        viewer = await h.role_id(lukas, "viewer")
        level = {"type": "account", "id": str(lukas.account_id)}
        _, anna = await h.subuser(lukas, "anna", [{"role_id": str(viewer), "level": level}])
        single = await h.client.delete(
            lukas.url(f"/api-tokens/{token['id']}"), headers=bearer(anna), params={"purge": "true"}
        )
        bulk = await h.client.post(lukas.url("/api-tokens/purge-revoked"), headers=bearer(anna))
        anonymous = await h.client.post(lukas.url("/api-tokens/purge-revoked"))
        names = await listed_names(h, lukas)
    assert single.status_code == 403
    assert bulk.status_code == 403
    assert anonymous.status_code == 401
    assert names == ["Altes Tablet"]


async def test_purging_is_isolated_per_account() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        mia = await h.register("mia")
        theirs = await make_token(h, lukas, "Lukas alt", revoke=True)
        mine = await make_token(h, mia, "Mia alt", revoke=True)
        # Mia against Lukas's account and token: the account never reveals itself.
        foreign_single = await h.client.delete(
            lukas.url(f"/api-tokens/{theirs['id']}"), headers=mia.headers, params={"purge": "true"}
        )
        foreign_bulk = await h.client.post(
            lukas.url("/api-tokens/purge-revoked"), headers=mia.headers
        )
        # Lukas's token through Mia's own account path: not hers, 404.
        cross_path = await h.client.delete(
            mia.url(f"/api-tokens/{theirs['id']}"), headers=mia.headers, params={"purge": "true"}
        )
        own_bulk = await h.client.post(mia.url("/api-tokens/purge-revoked"), headers=mia.headers)
        lukas_names = await listed_names(h, lukas)
        mia_names = await listed_names(h, mia)
        with pytest.raises(NotFoundError):
            await h.app.state.identity.tokens.purge(
                mia.account_id, UUID(theirs["id"]), owner_ctx(mia)
            )
    assert foreign_single.status_code == 404
    assert foreign_bulk.status_code == 404
    assert cross_path.status_code == 404
    assert own_bulk.json() == {"deleted": 1, "ids": [mine["id"]]}
    assert lukas_names == ["Lukas alt"]
    assert mia_names == []


async def test_purge_drops_tuples_left_over_from_a_revoked_token() -> None:
    """A tuple that survived a revoke (e.g. written by hand) would be invisible to the account
    reconcile once the row is gone: the purge reconciles the subject itself."""
    async with harness() as h:
        lukas = await h.register("lukas")
        token = await make_token(h, lukas, "Altes Tablet", revoke=True)
        stale = RelationshipTuple(api_token(UUID(token["id"])), "member", account(lukas.account_id))
        await h.authz.write([stale], [])
        before = h.tuples_of(f"api_token:{token['id']}")
        purged = await h.app.state.identity.tokens.purge_revoked(lukas.account_id, owner_ctx(lukas))
        after = h.tuples_of(f"api_token:{token['id']}")
    assert before == {f"account:{lukas.account_id}#member"}
    assert [t.id for t in purged] == [UUID(token["id"])]
    assert after == set()


async def test_purge_of_an_unknown_token_is_404() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        missing = await h.client.delete(
            lukas.url(f"/api-tokens/{uuid4()}"), headers=lukas.headers, params={"purge": "true"}
        )
    assert missing.status_code == 404
