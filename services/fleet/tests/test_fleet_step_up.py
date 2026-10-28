"""Step-up (ADR 0053): deleting a mower and removing a linked account need a recent confirmation."""

from datetime import UTC, datetime, timedelta
from typing import Any

from fleet_support import API, Harness, Person, harness
from mowdeck_auth.testing import bearer

REAUTH = "urn:mowdeck:problem:reauth-required"


def _as(h: Harness, owner: Person, **update: Any) -> dict[str, str]:
    return bearer(h.keys.mint(owner.principal.model_copy(update=update)))


async def test_fleet_delete_mower_needs_a_recent_confirmation() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower = await h.register(owner)
        url = f"{API}/mowers/{mower['id']}"
        old = datetime.now(UTC) - timedelta(minutes=30)

        never = await h.client.delete(url, headers=_as(h, owner, auth_time=None, auth_methods=[]))
        stale = await h.client.delete(url, headers=_as(h, owner, auth_time=old))
        assert (never.status_code, stale.status_code) == (401, 401)
        assert stale.json()["type"] == REAUTH
        assert "insufficient_user_authentication" in stale.headers["www-authenticate"]
        assert (await h.client.get(url, headers=owner.headers)).status_code == 200

        done = await h.client.delete(url, headers=owner.headers)
        assert done.status_code == 204


async def test_fleet_step_up_is_asked_only_after_the_permission_check() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower = await h.register(owner)
        url = f"{API}/mowers/{mower['id']}"

        # A stranger without the right gets 404 before any re-authentication is asked for.
        stranger = await h.owner()
        hidden = await h.client.delete(
            url, headers=_as(h, stranger, auth_time=None, auth_methods=[])
        )
        assert hidden.status_code == 404
