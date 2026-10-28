"""Setup tour progress (ADR 0062): ``GET`` and ``PUT /me/onboarding``."""

from datetime import timedelta
from typing import Any

import pytest
from identity_support import API, bearer, harness

URL = f"{API}/me/onboarding"


async def test_a_new_user_starts_untouched() -> None:
    async with harness() as h:
        owner = await h.register("tourist")
        response = await h.client.get(URL, headers=owner.headers)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "status": "new",
        "skipped_steps": [],
        "dismissed_hints": [],
        "updated_at": None,
    }


async def test_progress_is_stored_replaced_and_kept_per_user() -> None:
    async with harness() as h:
        owner = await h.register("tourist")
        other = await h.register("neighbour")
        first = await h.client.put(
            URL,
            headers=owner.headers,
            json={"status": "active", "skipped_steps": ["passkey", "passkey", "schedule"]},
        )
        h.clock.advance(timedelta(minutes=5))
        second = await h.client.put(
            URL,
            headers=owner.headers,
            json={
                "status": "completed",
                "skipped_steps": ["schedule"],
                "dismissed_hints": ["mower-map"],
            },
        )
        mine = (await h.client.get(URL, headers=owner.headers)).json()
        theirs = (await h.client.get(URL, headers=other.headers)).json()
    assert first.status_code == 200, first.text
    assert first.json()["skipped_steps"] == ["passkey", "schedule"]  # duplicates dropped
    assert second.status_code == 200, second.text
    assert mine["status"] == "completed"
    assert mine["skipped_steps"] == ["schedule"]
    assert mine["dismissed_hints"] == ["mower-map"]
    assert mine["updated_at"] > first.json()["updated_at"]
    assert theirs["status"] == "new"


async def test_a_subuser_has_a_tour_of_their_own() -> None:
    async with harness() as h:
        owner = await h.register("tourist")
        _, token = await h.subuser(owner, "helper")
        saved = await h.client.put(URL, headers=bearer(token), json={"status": "dismissed"})
        owners = (await h.client.get(URL, headers=owner.headers)).json()
    assert saved.status_code == 200, saved.text
    assert saved.json()["status"] == "dismissed"
    assert owners["status"] == "new"


@pytest.mark.parametrize(
    "body",
    [
        {"status": "new"},
        {"status": "finished"},
        {"status": "active", "skipped_steps": ["Passkey"]},
        {"status": "active", "skipped_steps": ["a" * 33]},
        {"status": "active", "dismissed_hints": ["map hint"]},
        {"status": "active", "dismissed_hints": [f"h{i}" for i in range(33)]},
        {"status": "active", "extra": True},
    ],
)
async def test_invalid_bodies_are_refused(body: dict[str, Any]) -> None:
    async with harness() as h:
        owner = await h.register("tourist")
        response = await h.client.put(URL, headers=owner.headers, json=body)
        after = (await h.client.get(URL, headers=owner.headers)).json()
    assert response.status_code == 422, response.text
    assert after["status"] == "new"


async def test_api_tokens_and_anonymous_callers_have_no_tour() -> None:
    async with harness() as h:
        owner = await h.register("tourist")
        created = await h.client.post(
            owner.url("/api-tokens"),
            headers=owner.headers,
            json={
                "name": "ha",
                "grants": [
                    {
                        "level": {"type": "account", "id": str(owner.account_id)},
                        "permissions": ["mower.view"],
                    }
                ],
            },
        )
        assert created.status_code == 201, created.text
        exchanged = await h.client.post(
            f"{API}/token", json={"grant_type": "api_token", "token": created.json()["token"]}
        )
        assert exchanged.status_code == 200, exchanged.text
        token = exchanged.json()["access_token"]
        read = await h.client.get(URL, headers=bearer(token))
        write = await h.client.put(URL, headers=bearer(token), json={"status": "active"})
        anonymous = await h.client.get(URL)
    assert read.status_code == 403
    assert write.status_code == 403
    assert anonymous.status_code == 401


async def test_the_progress_goes_with_a_deleted_subuser() -> None:
    async with harness() as h:
        owner = await h.register("tourist")
        helper_id, token = await h.subuser(owner, "helper")
        await h.client.put(URL, headers=bearer(token), json={"status": "active"})
        async with h.store.transaction() as tx:
            before = await tx.onboarding.count(user_id=helper_id)
        removed = await h.client.delete(owner.url(f"/users/{helper_id}"), headers=owner.headers)
        async with h.store.transaction() as tx:
            after = await tx.onboarding.count(user_id=helper_id)
    assert before == 1
    assert removed.status_code == 204, removed.text
    assert after == 0
